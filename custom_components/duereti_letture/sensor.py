"""Sensori diagnostici: i dati veri finiscono in external statistics (statistics.py).
Questi sensori servono solo a vedere a colpo d'occhio lo stato dell'import."""
from __future__ import annotations

from datetime import date

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_PODS, DOMAIN


def _device_info_account(entry: ConfigEntry) -> DeviceInfo:
    """Dispositivo 'account': raggruppa i sensori generali dell'istanza."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Duereti",
        model="Account API",
    )


def _device_info_pod(entry: ConfigEntry, pod: str) -> DeviceInfo:
    """Dispositivo per un singolo POD, agganciato all'account come 'via_device'."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{pod}")},
        name=f"POD {pod}",
        manufacturer="Duereti",
        model="Punto di prelievo",
        via_device=(DOMAIN, entry.entry_id),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    pods = entry.data.get(CONF_PODS, [])

    entità = [
        DuretiUltimoImportSensor(coordinator, entry),
        DuretiStatoAttesaSensor(coordinator, entry),
        DuretiPodConfiguratiSensor(coordinator, entry, pods),
    ]
    for pod_conf in pods:
        entità.append(DuretiUltimaDataDisponibileSensor(coordinator, entry, pod_conf["pod"]))
        entità.append(DuretiConsumoPeriodoSensor(coordinator, entry, pod_conf["pod"]))

    async_add_entities(entità)


class DuretiUltimoImportSensor(CoordinatorEntity, SensorEntity):
    """Mostra la data dell'ultimo import di curve riuscito."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Duereti - Ultimo import"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ultimo_import"
        self._attr_device_info = _device_info_account(entry)

    @property
    def native_value(self):
        if self.coordinator.data:
            return self.coordinator.data.get("ultimo_aggiornamento")
        return None

    @property
    def extra_state_attributes(self):
        if self.coordinator.data:
            return {
                "pod_aggiornati": self.coordinator.data.get("pod_aggiornati"),
                "stato": self.coordinator.data.get("stato"),
                "ultimo_errore": self.coordinator.data.get("ultimo_errore"),
            }
        return {}


class DuretiUltimaDataDisponibileSensor(CoordinatorEntity, SensorEntity):
    """Mostra l'ultima data per cui sono realmente arrivati dati curva per un POD.

    A differenza di 'Duereti - Ultimo import' (che riflette solo quando è
    girato l'ultimo ciclo con successo), questo legge lo stato reale delle
    external statistics per il POD: se l'ultima richiesta ha restituito dati
    solo fino a metà periodo, questo sensore lo riflette correttamente.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator, entry: ConfigEntry, pod: str) -> None:
        super().__init__(coordinator)
        self._pod = pod
        self._attr_unique_id = f"{entry.entry_id}_{pod}_ultima_data_disponibile"
        self._attr_name = "Ultima data disponibile"
        self._attr_device_info = _device_info_pod(entry, pod)

    @property
    def native_value(self) -> date | None:
        if not self.coordinator.data:
            return None
        valore = self.coordinator.data.get("ultime_date_per_pod", {}).get(self._pod)
        if valore is None:
            return None
        return date.fromisoformat(valore)


class DuretiConsumoPeriodoSensor(CoordinatorEntity, SensorEntity):
    """Mostra il consumo totale (kWh) dell'ultimo periodo importato per un POD.

    Non è una statistica progressiva come le external statistics: è solo un
    numero di riepilogo dell'ultimo import riuscito, comodo per un colpo
    d'occhio senza aprire il grafico della Energy Dashboard.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, entry: ConfigEntry, pod: str) -> None:
        super().__init__(coordinator)
        self._pod = pod
        self._attr_unique_id = f"{entry.entry_id}_{pod}_consumo_periodo"
        self._attr_name = "Consumo ultimo periodo"
        self._attr_device_info = _device_info_pod(entry, pod)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("totale_kwh_periodo_per_pod", {}).get(self._pod)

    @property
    def extra_state_attributes(self):
        if self.coordinator.data:
            return {"periodo": self.coordinator.data.get("periodo_importato")}
        return {}


class DuretiStatoAttesaSensor(SensorEntity):
    """Minuti di attesa del file corrente (requestResult in polling).

    Utile per automazioni/notifiche: se resta alto per troppo tempo (ore),
    può segnalare un problema (WAF, job bloccato lato Duereti, ecc.).
    Si aggiorna da solo ogni 30s (should_poll) perché durante l'attesa il
    coordinator non emette aggiornamenti push.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:timer-sand"
    _attr_native_unit_of_measurement = "min"
    _attr_should_poll = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_stato_attesa"
        self._attr_name = "Duereti - Attesa file (minuti)"
        self._attr_native_value = 0
        self._attr_device_info = _device_info_account(entry)

    async def async_update(self) -> None:
        pending_since = getattr(self.coordinator, "pending_since", None)
        if pending_since is None:
            self._attr_native_value = 0
        else:
            delta = dt_util.utcnow() - pending_since
            self._attr_native_value = round(delta.total_seconds() / 60)

    @property
    def extra_state_attributes(self):
        return {
            "in_attesa": getattr(self.coordinator, "pending_since", None) is not None,
            "ticket": getattr(self.coordinator, "pending_ticket", None),
            "in_attesa_dal": (
                self.coordinator.pending_since.isoformat()
                if getattr(self.coordinator, "pending_since", None)
                else None
            ),
        }


class DuretiPodConfiguratiSensor(SensorEntity):
    """Mostra quanti POD sono configurati in questa istanza dell'integrazione."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator, entry: ConfigEntry, pods: list[dict]) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_pod_configurati"
        self._attr_name = "Duereti - POD configurati"
        self._attr_native_value = len(pods)
        self._pods = [p["pod"] for p in pods]
        self._attr_device_info = _device_info_account(entry)

    @property
    def extra_state_attributes(self):
        return {"pods": self._pods}
