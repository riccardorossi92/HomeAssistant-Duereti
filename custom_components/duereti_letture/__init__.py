"""Integrazione Home Assistant per l'estrazione curve/letture da Duereti."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import DuretiApiError, DuretiAuthError
from .const import CONF_CLIENT_ID, CONF_PODS, CONF_SECRET_ID, DOMAIN
from .coordinator import DuretiCoordinator

_LOGGER = logging.getLogger(__name__)

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
    return unload_ok
