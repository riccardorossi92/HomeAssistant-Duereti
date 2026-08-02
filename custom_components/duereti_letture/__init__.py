"""Integrazione Home Assistant per l'estrazione curve/letture da Duereti."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_PODS, CONF_SECRET_ID, DOMAIN
from .coordinator import DuretiCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Inizializza l'integrazione a partire da una config entry."""
    coordinator = DuretiCoordinator(
        hass,
        entry,
        client_id=entry.data[CONF_CLIENT_ID],
        secret_id=entry.data[CONF_SECRET_ID],
        pods=entry.data[CONF_PODS],
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Se la config entry cambia (es. un nuovo POD aggiunto dalle Opzioni),
    # ricarichiamo l'integrazione: serve perché il coordinator riprenda la
    # lista POD aggiornata e sensor.py crei l'entità per il nuovo POD.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ricarica la config entry quando i suoi dati cambiano."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica la config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
