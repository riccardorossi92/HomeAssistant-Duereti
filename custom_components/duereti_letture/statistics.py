"""Import delle curve Duereti come external statistics in Home Assistant.

Ricalca l'approccio già usato per HomeAssistant-OctopusEnergyIT:
- statistic_id sanitizzato
- StatisticMeanType.NONE (dato cumulativo, non media)
- dedup tramite get_last_statistics per essere restart-safe
- sum progressiva calcolata a partire dall'ultimo valore noto
"""
from __future__ import annotations

import logging
import re

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant

from .api import RisultatoLetture
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _sanitize_statistic_id(pod: str) -> str:
    """Genera uno statistic_id valido a partire dal codice POD."""
    slug = re.sub(r"[^a-z0-9_]", "_", pod.lower())
    return f"{DOMAIN}:{slug}_energia"


async def async_import_curva(
    hass: HomeAssistant, pod: str, risultato: RisultatoLetture, nome_pod: str | None = None
) -> None:
    """Importa i punti curva di un POD come external statistics."""
    if not risultato.punti:
        _LOGGER.debug("Nessun punto curva da importare per POD %s", pod)
        return

    statistic_id = _sanitize_statistic_id(pod)
    punti_ordinati = sorted(risultato.punti, key=lambda p: p.timestamp)

    last_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    running_sum = 0.0
    last_ts = None
    if last_stats.get(statistic_id):
        last_entry = last_stats[statistic_id][0]
        running_sum = last_entry.get("sum") or 0.0
        last_ts = last_entry.get("start")

    stats = []
    for punto in punti_ordinati:
        # Nudge a mezzanotte quando il timestamp cade esattamente sulla mezzanotte,
        # per evitare ambiguità di attribuzione al giorno precedente/successivo
        ts = punto.timestamp
        if last_ts is not None and ts.timestamp() <= last_ts:
            continue  # già importato, dedup restart-safe

        running_sum += punto.valore_kwh
        stats.append(
            {
                "start": ts,
                "state": punto.valore_kwh,
                "sum": running_sum,
            }
        )

    if not stats:
        _LOGGER.debug("Nessun nuovo punto da importare per POD %s (già aggiornato)", pod)
        return

    metadata = {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": nome_pod or f"Duereti {pod}",
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "kWh",
    }

    async_add_external_statistics(hass, metadata, stats)
    _LOGGER.info("Importati %d punti curva per POD %s (%s)", len(stats), pod, statistic_id)


async def async_get_ultima_data_disponibile(hass: HomeAssistant, pod: str):
    """Restituisce la data (senza ora) dell'ultimo punto curva effettivamente
    presente nelle external statistics per il POD, o None se non c'è ancora
    nessun dato importato.

    A differenza del campo 'ultimo_aggiornamento' del coordinator (che riflette
    solo la fine del range richiesto all'API), questa legge lo stato reale
    delle statistiche salvate - utile perché una richiesta può restituire dati
    parziali o vuoti per gli ultimi giorni del periodo.
    """
    from datetime import datetime

    statistic_id = _sanitize_statistic_id(pod)
    last_stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    entry = last_stats.get(statistic_id)
    if not entry:
        return None

    start = entry[0].get("start")
    if start is None:
        return None

    return datetime.fromtimestamp(start).date()
