"""Integrazione Home Assistant per l'estrazione curve/letture da Duereti."""
from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .api import DuretiApiError, DuretiAuthError
from .const import CONF_CLIENT_ID, CONF_PODS, CONF_SECRET_ID, DOMAIN
from .coordinator import DuretiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_RECUPERA_TICKET = "recupera_ticket"

SCHEMA_RECUPERA_TICKET = vol.Schema(
    {
        vol.Required("ticket"): cv.string,
        vol.Optional("data_da"): cv.date,
        vol.Optional("data_a"): cv.date,
        vol.Optional("entry_id"): cv.string,
    }
)


def _trova_coordinator(hass: HomeAssistant, entry_id: str | None) -> DuretiCoordinator:
    """Individua il coordinator su cui agire.

    Con una sola istanza dell'integrazione entry_id è superfluo; con più
    istanze va indicato, altrimenti non sapremmo su quale agire.
    """
    coordinators: dict = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("Nessuna istanza di Duereti Letture configurata.")

    if entry_id:
        if entry_id not in coordinators:
            raise HomeAssistantError(
                f"Nessuna istanza con entry_id '{entry_id}'. Disponibili: {list(coordinators)}"
            )
        return coordinators[entry_id]

    if len(coordinators) > 1:
        raise HomeAssistantError(
            "Ci sono più istanze di Duereti Letture configurate: specifica 'entry_id'. "
            f"Disponibili: {list(coordinators)}"
        )
    return next(iter(coordinators.values()))


async def _async_registra_servizi(hass: HomeAssistant) -> None:
    """Registra le azioni dell'integrazione (una sola volta)."""
    if hass.services.has_service(DOMAIN, SERVICE_RECUPERA_TICKET):
        return

    async def _recupera_ticket(call: ServiceCall) -> None:
        coordinator = _trova_coordinator(hass, call.data.get("entry_id"))
        data_da: date | None = call.data.get("data_da")
        data_a: date | None = call.data.get("data_a")
        await coordinator.async_forza_ticket(call.data["ticket"], data_da, data_a)

    hass.services.async_register(
        DOMAIN, SERVICE_RECUPERA_TICKET, _recupera_ticket, schema=SCHEMA_RECUPERA_TICKET
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Inizializza l'integrazione a partire da una config entry."""
    coordinator = DuretiCoordinator(
        hass,
        entry,
        client_id=entry.data[CONF_CLIENT_ID],
        secret_id=entry.data[CONF_SECRET_ID],
        pods=entry.data[CONF_PODS],
    )

    # Il setup dipende SOLO dalla capacità di autenticarsi. Il recupero dei
    # dati (requestExport/requestResult) è un'operazione lunga e soggetta ai
    # blocchi intermittenti del WAF Duereti: un suo fallimento deve marcare
    # come fallito il singolo aggiornamento, non l'intera integrazione.
    try:
        await coordinator.api.async_validate_credentials()
    except DuretiAuthError as err:
        raise ConfigEntryAuthFailed(f"Credenziali Duereti non valide: {err}") from err
    except DuretiApiError as err:
        raise ConfigEntryNotReady(f"Impossibile contattare le API Duereti: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _async_registra_servizi(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # async_refresh (non async_config_entry_first_refresh) perché non deve
    # sollevare ConfigEntryNotReady in caso di errore: l'integrazione resta
    # caricata con le sue entità, il coordinator segna l'aggiornamento come
    # fallito e HA riproverà da solo al ciclo successivo.
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        _LOGGER.warning(
            "Primo aggiornamento dei dati non riuscito (l'autenticazione però funziona): "
            "l'integrazione resta attiva e riproverà automaticamente. Ultimo errore: %s",
            coordinator.last_exception,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica la config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # Il servizio è registrato a livello di dominio, non di entry:
        # va rimosso solo quando non resta più nessuna istanza.
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_RECUPERA_TICKET)
    return unload_ok
