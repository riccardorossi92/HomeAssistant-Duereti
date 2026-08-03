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


# Interpretazione della colonna FL_ORA_LEGALE del CSV Duereti.
#
# ATTENZIONE, questa mappatura è un'inferenza, non un dato confermato dal
# manuale: negli export reali visti finora (maggio-luglio, tutti in ora
# legale) il valore era sempre "2". Da lì l'ipotesi che il flag indichi
# direttamente l'offset UTC in ore: 2 = ora legale (CEST, UTC+2),
# 1 = ora solare (CET, UTC+1).
#
# Se il flag ha un valore diverso da quelli previsti si ricade sul fuso
# locale di Home Assistant (comportamento precedente), loggando un warning
# una sola volta: così un'ipotesi sbagliata degrada in modo prevedibile
# invece di produrre silenziosamente timestamp errati.
_OFFSET_DA_FLAG = {
    "1": 1,  # ora solare (CET)
    "2": 2,  # ora legale (CEST)
}

_flag_sconosciuti_segnalati: set[str] = set()


def _timestamp_aware(punto) -> "datetime":  # noqa: F821
    """Rende timezone-aware il timestamp naive di un punto curva.

    Usa FL_ORA_LEGALE quando è un valore noto, perché è l'unico modo di
    distinguere le due ore identiche del cambio ora d'autunno. Altrimenti
    ricade sul fuso locale, che in quell'unica ora all'anno è ambiguo.
    """
    from datetime import timedelta, timezone

    ts = punto.timestamp
    if ts.tzinfo is not None:
        return ts

    flag = getattr(punto, "ora_legale", None)
    offset = _OFFSET_DA_FLAG.get(flag) if flag else None

    if offset is not None:
        return ts.replace(tzinfo=timezone(timedelta(hours=offset)))

    if flag and flag not in _flag_sconosciuti_segnalati:
        _flag_sconosciuti_segnalati.add(flag)
        _LOGGER.warning(
            "Valore FL_ORA_LEGALE non riconosciuto (%r): uso il fuso locale come ripiego. "
            "Segnalalo come issue: serve a gestire correttamente il cambio ora.",
            flag,
        )
    return ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)


def _aggrega_per_ora(punti: list) -> list[tuple]:
    """Aggrega i punti curva (intervalli di 15 minuti) in bucket orari.

    Due motivi:

    1. Le external statistics di Home Assistant sono ORARIE: 'start' deve
       cadere sull'inizio dell'ora. Passare timestamp a :15/:30/:45 farebbe
       finire più punti nella stessa ora, sovrascrivendosi a vicenda.
    2. I timestamp del CSV Duereti sono NAIVE (nessun fuso orario) e riferiti
       all'ora locale italiana. HA li rifiuta con "Naive timestamp: no or
       invalid timezone info provided", quindi vanno resi timezone-aware.

    L'aggregazione avviene sull'istante assoluto (UTC), non sull'ora locale:
    nell'ora ripetuta del cambio ora due istanti diversi hanno lo stesso
    orario locale, e raggrupparli per orario locale li fonderebbe. In UTC
    restano distinti, quindi diventano due ore separate come dev'essere.

    Restituisce una lista di (inizio_ora_utc_aware, kwh_totali) ordinata.
    """
    bucket: dict = defaultdict(float)

    for punto in punti:
        ts_utc = dt_util.as_utc(_timestamp_aware(punto))
        inizio_ora = ts_utc.replace(minute=0, second=0, microsecond=0)
        bucket[inizio_ora] += punto.valore_kwh

    return sorted(bucket.items())


async def async_import_curva(
    hass: HomeAssistant, pod: str, risultato: RisultatoLetture, nome_pod: str | None = None
) -> "date | None":  # noqa: F821
    """Importa i punti curva di un POD come external statistics.

    Restituisce la data (locale) dell'ultimo punto effettivamente importato,
    oppure None se non è stato importato nulla. Il chiamante la usa per
    aggiornare subito i sensori diagnostici: async_add_external_statistics
    accoda la scrittura al recorder invece di eseguirla immediatamente, quindi
    rileggere il database subito dopo restituirebbe ancora i dati vecchi.
    """
    if not risultato.punti:
        _LOGGER.debug("Nessun punto curva da importare per POD %s", pod)
        return None

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
    scartati = 0
    for inizio_ora, kwh in ore:
        if last_ts is not None and inizio_ora.timestamp() <= last_ts:
            scartati += 1
            continue  # già importato, dedup restart-safe

        running_sum += kwh
        stats.append(
            {
                "start": inizio_ora,
                "state": kwh,
                "sum": running_sum,
            }
        )

    if scartati:
        _LOGGER.debug(
            "POD %s: %d ore su %d scartate perché precedenti all'ultimo punto già "
            "importato (%s)",
            pod,
            scartati,
            len(ore),
            dt_util.as_local(dt_util.utc_from_timestamp(last_ts)).isoformat(),
        )

    if not stats:
        if scartati and ore and last_ts is not None and ore[-1][0].timestamp() < last_ts:
            # Tutti i punti del file finiscono PRIMA dell'ultimo punto già
            # presente. Attenzione: conosciamo solo l'ultimo timestamp
            # importato, non il primo, quindi non possiamo sapere se quel
            # periodo era già stato importato (ri-import innocuo) o se è un
            # buco storico vero. Segnaliamo la cosa senza affermare quale dei
            # due sia, spiegando cosa fare nel caso peggiore.
            _LOGGER.warning(
                "POD %s: il file copre un periodo (%s - %s) che finisce prima dell'ultimo "
                "dato già presente (%s), quindi non è stato importato nulla. Se quel "
                "periodo era già stato importato puoi ignorare questo messaggio. Se invece "
                "stai cercando di colmare un buco storico, la somma progressiva delle "
                "statistiche non permette di inserire dati più vecchi: occorre cancellare "
                "le statistiche di %s da Impostazioni > Sistema > Statistiche e "
                "reimportare in ordine cronologico.",
                pod,
                dt_util.as_local(ore[0][0]).isoformat(),
                dt_util.as_local(ore[-1][0]).isoformat(),
                dt_util.as_local(dt_util.utc_from_timestamp(last_ts)).isoformat(),
                statistic_id,
            )
        else:
            _LOGGER.debug("Nessun nuovo punto da importare per POD %s (già aggiornato)", pod)
        # Nulla di nuovo importato ora, ma se c'erano già dati la data
        # disponibile resta quella dell'ultimo punto presente: la restituiamo
        # comunque, così il sensore non torna a "Sconosciuto".
        if last_ts is not None:
            return dt_util.as_local(dt_util.utc_from_timestamp(last_ts)).date()
        return None

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
    ultima_data = dt_util.as_local(stats[-1]["start"]).date()
    _LOGGER.info(
        "Importati %d punti curva per POD %s (%s), ultimo punto: %s",
        len(stats),
        pod,
        statistic_id,
        ultima_data.isoformat(),
    )
    return ultima_data


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
