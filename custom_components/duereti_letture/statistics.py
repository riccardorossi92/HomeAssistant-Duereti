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
from collections import defaultdict

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import RisultatoLetture
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _sanitize_statistic_id(pod: str) -> str:
    """Genera uno statistic_id valido a partire dal codice POD."""
    slug = re.sub(r"[^a-z0-9_]", "_", pod.lower())
    return f"{DOMAIN}:{slug}_energia"


def _aggrega_per_ora(punti: list) -> list[tuple]:
    """Aggrega i punti curva (intervalli di 15 minuti) in bucket orari.

    Due motivi:

    1. Le external statistics di Home Assistant sono ORARIE: 'start' deve
       cadere sull'inizio dell'ora. Passare timestamp a :15/:30/:45 farebbe
       finire più punti nella stessa ora, sovrascrivendosi a vicenda.
    2. I timestamp del CSV Duereti sono NAIVE (nessun fuso orario) e riferiti
       all'ora locale italiana. HA li rifiuta con "Naive timestamp: no or
       invalid timezone info provided", quindi vanno resi timezone-aware.

    NOTA sull'ora legale: il CSV ha una colonna FL_ORA_LEGALE che qui non
    usiamo. Nell'ora ripetuta del cambio ora (l'ultima domenica di ottobre)
    due intervalli diversi possono avere lo stesso orario locale; in quel caso
    finiscono nello stesso bucket e i loro consumi vengono sommati. È il
    comportamento meno sbagliato senza interpretare il flag, e riguarda una
    sola ora all'anno.

    Restituisce una lista di (inizio_ora_aware, kwh_totali_nell_ora) ordinata.
    """
    bucket: dict = defaultdict(float)

    for punto in punti:
        ts = punto.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        else:
            ts = dt_util.as_local(ts)
        inizio_ora = ts.replace(minute=0, second=0, microsecond=0)
        bucket[inizio_ora] += punto.valore_kwh

    return sorted(bucket.items())


async def async_import_curva(
    hass: HomeAssistant, pod: str, risultato: RisultatoLetture, nome_pod: str | None = None
) -> None:
    """Importa i punti curva di un POD come external statistics."""
    if not risultato.punti:
        _LOGGER.debug("Nessun punto curva da importare per POD %s", pod)
        return

    statistic_id = _sanitize_statistic_id(pod)
    ore = _aggrega_per_ora(risultato.punti)
    _LOGGER.debug(
        "POD %s: %d punti a 15 minuti aggregati in %d ore (%s -> %s)",
        pod,
        len(risultato.punti),
        len(ore),
        ore[0][0].isoformat() if ore else "-",
        ore[-1][0].isoformat() if ore else "-",
    )

    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    running_sum = 0.0
    last_ts = None
    if last_stats.get(statistic_id):
        last_entry = last_stats[statistic_id][0]
        running_sum = last_entry.get("sum") or 0.0
        last_ts = last_entry.get("start")

    stats = []
    for inizio_ora, kwh in ore:
        if last_ts is not None and inizio_ora.timestamp() <= last_ts:
            continue  # già importato, dedup restart-safe

        running_sum += kwh
        stats.append(
            {
                "start": inizio_ora,
                "state": kwh,
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
        "unit_class": "energy",
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
    statistic_id = _sanitize_statistic_id(pod)
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    entry = last_stats.get(statistic_id)
    if not entry:
        return None

    start = entry[0].get("start")
    if start is None:
        return None

    # 'start' è un timestamp UTC: lo convertiamo nel fuso locale prima di
    # ricavarne la data, altrimenti a cavallo della mezzanotte si otterrebbe
    # il giorno sbagliato.
    return dt_util.as_local(dt_util.utc_from_timestamp(start)).date()
