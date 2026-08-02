"""DataUpdateCoordinator per l'integrazione Duereti Letture."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import DuretiApiClient, DuretiApiError, DuretiAuthError
from .const import (
    CONF_BACKFILL_DONE,
    CONF_PENDING_DATA_A,
    CONF_PENDING_DATA_DA,
    CONF_PENDING_IS_BACKFILL,
    CONF_PENDING_TICKET,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MODE_CURVE,
)
from .statistics import async_get_ultima_data_disponibile, async_import_curva

_LOGGER = logging.getLogger(__name__)


def _mese_precedente_completo(oggi: date) -> tuple[date, date]:
    """Calcola primo/ultimo giorno del mese precedente a 'oggi'.

    Le curve/misure dei distributori vengono in genere validate e chiuse a
    fine mese, non giorno per giorno: chiedere dati di pochi giorni fa
    (mese ancora in corso) rischia di far restare il job in coda a tempo
    indeterminato perché quei dati semplicemente non esistono ancora.
    """
    primo_giorno_mese_corrente = oggi.replace(day=1)
    ultimo_giorno_mese_precedente = primo_giorno_mese_corrente - timedelta(days=1)
    primo_giorno_mese_precedente = ultimo_giorno_mese_precedente.replace(day=1)
    return primo_giorno_mese_precedente, ultimo_giorno_mese_precedente


def _range_sei_mesi_precedenti(oggi: date) -> tuple[date, date]:
    """Calcola il range degli ultimi 6 mesi completi (per l'iniezione iniziale).

    Va dal primo giorno del mese 6 mesi prima dell'ultimo mese completo,
    fino all'ultimo giorno del mese precedente a 'oggi'. Esattamente al
    limite di 6 mesi dichiarato dal manuale per requestExport.
    """
    _, fine = _mese_precedente_completo(oggi)
    anno, mese = fine.year, fine.month - 5
    while mese <= 0:
        mese += 12
        anno -= 1
    inizio = date(anno, mese, 1)
    return inizio, fine


class DuretiCoordinator(DataUpdateCoordinator):
    """Coordina il download mensile delle curve e il loro import come statistiche.

    Al primo avvio (config entry senza CONF_BACKFILL_DONE) fa un'unica
    richiesta per gli ultimi 6 mesi completi, come iniezione storica
    iniziale. Dai run successivi, richiede un mese alla volta (il mese
    precedente completo), una volta al mese.

    requestExport è rapida e resta nel ciclo normale del coordinator.
    requestResult può invece restare in coda per ore (polling ogni ~10 minuti,
    vedi const.RESULT_POLL_INTERVAL_SECONDS/MAX_ATTEMPTS): bloccare qui il
    coordinator farebbe fallire il primo setup dell'integrazione, che HA
    considera fallito dopo pochi minuti di attesa. Il polling+import viene
    quindi eseguito in un task in background, disaccoppiato dal ciclo del
    coordinator.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client_id: str,
        secret_id: str,
        pods: list[dict],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=DEFAULT_SCAN_INTERVAL_HOURS),
        )
        self._entry = entry
        self._pods = pods
        session = async_get_clientsession(hass)
        self.api = DuretiApiClient(session, client_id, secret_id)
        self._background_task = None
        self._ultima_richiesta: str | None = None  # chiave del periodo già richiesto
        self.pending_since: datetime | None = None  # per il sensore di stato/attesa
        self.pending_ticket: str | None = None

    async def _async_update_data(self) -> dict:
        if self._background_task and not self._background_task.done():
            _LOGGER.debug("Import precedente ancora in corso, salto questo ciclo")
            return await self._con_ultime_date(
                self.data or {"stato": "import precedente ancora in corso"}
            )

        ticket_pendente = self._entry.data.get(CONF_PENDING_TICKET)
        if ticket_pendente:
            # C'era già un ticket ottenuto da un requestExport riuscito prima
            # di un reload/riavvio: lo riprendiamo direttamente invece di
            # rifare requestExport da capo (che rischierebbe di essere
            # bloccato dal WAF proprio mentre Duereti sta già lavorando sul
            # ticket precedente).
            _LOGGER.info("Riprendo il ticket %s salvato da un ciclo precedente", ticket_pendente)
            data_da = date.fromisoformat(self._entry.data[CONF_PENDING_DATA_DA])
            data_a = date.fromisoformat(self._entry.data[CONF_PENDING_DATA_A])
            is_backfill = bool(self._entry.data.get(CONF_PENDING_IS_BACKFILL, False))

            self.pending_since = dt_util.utcnow()
            self.pending_ticket = ticket_pendente
            self._background_task = self.hass.async_create_task(
                self._poll_and_import(ticket_pendente, data_da, data_a, is_backfill),
                name=f"{DOMAIN}_poll_import_ripreso_{ticket_pendente}",
            )
            return await self._con_ultime_date(
                {
                    "stato": "ticket precedente ripreso, in attesa del file",
                    "ticket": ticket_pendente,
                    "periodo": f"{data_da.isoformat()} - {data_a.isoformat()}",
                    "backfill": is_backfill,
                }
            )

        oggi = date.today()
        is_backfill = not self._entry.data.get(CONF_BACKFILL_DONE)

        if is_backfill:
            data_da, data_a = _range_sei_mesi_precedenti(oggi)
            chiave = f"backfill:{data_da.isoformat()}_{data_a.isoformat()}"
        else:
            data_da, data_a = _mese_precedente_completo(oggi)
            chiave = data_da.strftime("%Y-%m")

        if chiave == self._ultima_richiesta:
            _LOGGER.debug("Periodo %s già richiesto (in questa sessione), nessuna nuova richiesta", chiave)
            return await self._con_ultime_date(
                self.data or {"stato": f"periodo {chiave} già richiesto"}
            )

        if await self._periodo_gia_coperto(data_a):
            # self._ultima_richiesta vive solo in memoria e si perde ad ogni
            # reload/retry del setup (es. dopo un fallimento causato dal WAF):
            # una nuova istanza del coordinator non ha memoria di richieste
            # riuscite in precedenza. Controlliamo quindi anche lo stato
            # reale e persistente delle external statistics: se copre già
            # il periodo richiesto, evitiamo del tutto la chiamata API,
            # invece di rifarla inutilmente ad ogni retry.
            _LOGGER.debug(
                "Periodo %s già coperto dalle statistiche esistenti (verificato su dati "
                "persistenti, non solo in memoria): nessuna nuova richiesta necessaria",
                chiave,
            )
            self._ultima_richiesta = chiave
            return await self._con_ultime_date(
                self.data or {"stato": f"periodo {chiave} già coperto dai dati esistenti"}
            )

        try:
            ticket = await self.api.request_export(data_da, data_a, self._pods, mode=MODE_CURVE)
        except DuretiAuthError as err:
            # Solleva ConfigEntryAuthFailed: HA lo gestisce da solo avviando
            # automaticamente il flusso di reauth con async_step_reauth.
            raise ConfigEntryAuthFailed(f"Credenziali non valide: {err}") from err
        except DuretiApiError as err:
            raise UpdateFailed(f"Errore chiamando requestExport: {err}") from err

        self._ultima_richiesta = chiave
        self.pending_since = dt_util.utcnow()
        self.pending_ticket = ticket

        nuovi_dati = {
            **self._entry.data,
            CONF_PENDING_TICKET: ticket,
            CONF_PENDING_DATA_DA: data_da.isoformat(),
            CONF_PENDING_DATA_A: data_a.isoformat(),
            CONF_PENDING_IS_BACKFILL: is_backfill,
        }
        self.hass.config_entries.async_update_entry(self._entry, data=nuovi_dati)

        self._background_task = self.hass.async_create_task(
            self._poll_and_import(ticket, data_da, data_a, is_backfill),
            name=f"{DOMAIN}_poll_import_{chiave}",
        )

        return await self._con_ultime_date(
            {
                "stato": "richiesta inviata, in attesa del file",
                "ticket": ticket,
                "periodo": f"{data_da.isoformat()} - {data_a.isoformat()}",
                "backfill": is_backfill,
            }
        )

    async def _periodo_gia_coperto(self, data_a: date) -> bool:
        """True se TUTTI i POD configurati hanno già dati persistenti (nelle
        external statistics) che coprono almeno fino a 'data_a'.

        Usa lo stesso stato che legge il sensore 'Ultima data disponibile',
        quindi sopravvive a reload/riavvii - a differenza di
        self._ultima_richiesta, che è solo in memoria.
        """
        if not self._pods:
            return False
        for pod_conf in self._pods:
            data_disp = await async_get_ultima_data_disponibile(self.hass, pod_conf["pod"])
            if data_disp is None or data_disp < data_a:
                return False
        return True

    async def _con_ultime_date(self, dati: dict) -> dict:
        """Aggiunge al dict 'ultime_date_per_pod' leggendo lo stato reale
        delle external statistics, indipendentemente da nuove richieste."""
        ultime_date = {}
        for pod_conf in self._pods:
            pod = pod_conf["pod"]
            data_disp = await async_get_ultima_data_disponibile(self.hass, pod)
            if data_disp is not None:
                ultime_date[pod] = data_disp.isoformat()
        return {**dati, "ultime_date_per_pod": ultime_date}

    def _avvia_reauth(self) -> None:
        """Avvia il flusso di reauth manualmente: serve perché questo viene
        chiamato dal task in background (_poll_and_import), che non passa
        dal ciclo normale del coordinator e quindi non beneficia della
        gestione automatica di ConfigEntryAuthFailed."""
        if hasattr(self._entry, "async_start_reauth"):
            self._entry.async_start_reauth(self.hass)
        else:  # fallback per versioni HA più datate
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={
                        "source": config_entries.SOURCE_REAUTH,
                        "entry_id": self._entry.entry_id,
                    },
                    data=self._entry.data,
                )
            )

    def _pulisci_ticket_pendente(self) -> None:
        """Rimuove il ticket persistito dalla config entry: usato quando il
        polling è finito (successo o errore definitivo), così un eventuale
        reload successivo non lo trovi più e non tenti di riprenderlo."""
        if CONF_PENDING_TICKET not in self._entry.data:
            return
        nuovi_dati = dict(self._entry.data)
        nuovi_dati.pop(CONF_PENDING_TICKET, None)
        nuovi_dati.pop(CONF_PENDING_DATA_DA, None)
        nuovi_dati.pop(CONF_PENDING_DATA_A, None)
        nuovi_dati.pop(CONF_PENDING_IS_BACKFILL, None)
        self.hass.config_entries.async_update_entry(self._entry, data=nuovi_dati)

    async def _poll_and_import(
        self, ticket: str, data_da: date, data_a: date, is_backfill: bool
    ) -> None:
        """Task in background: aspetta il file (anche per ore) e importa i dati."""
        try:
            zip_bytes = await self.api.request_result(ticket)
        except DuretiAuthError as err:
            _LOGGER.error("Credenziali non valide durante il polling: %s", err)
            self.pending_since = None
            self.pending_ticket = None
            self._pulisci_ticket_pendente()
            self._avvia_reauth()
            dati = await self._con_ultime_date(
                {**(self.data or {}), "stato": f"credenziali non valide: {err}", "ultimo_errore": str(err)}
            )
            self.async_set_updated_data(dati)
            return
        except DuretiApiError as err:
            _LOGGER.error("Errore recuperando il file per il ticket %s: %s", ticket, err)
            self.pending_since = None
            self.pending_ticket = None
            self._pulisci_ticket_pendente()
            dati = await self._con_ultime_date(
                {**(self.data or {}), "stato": f"errore: {err}", "ultimo_errore": str(err)}
            )
            self.async_set_updated_data(dati)
            return

        from .api import parse_curve_zip

        risultati = parse_curve_zip(zip_bytes)

        totali_kwh = {
            pod: round(sum(p.valore_kwh for p in ris.punti), 3) for pod, ris in risultati.items()
        }

        for pod_conf in self._pods:
            pod = pod_conf["pod"]
            risultato = risultati.get(pod)
            if risultato is None:
                _LOGGER.warning(
                    "Nessun dato ricevuto per POD %s (periodo %s - %s)", pod, data_da, data_a
                )
                continue
            await async_import_curva(self.hass, pod, risultato)

        if is_backfill:
            _LOGGER.info(
                "Iniezione storica iniziale completata (%s - %s): la prossima richiesta sarà "
                "il mese precedente, una volta al mese",
                data_da,
                data_a,
            )
            nuovi_dati = {**self._entry.data, CONF_BACKFILL_DONE: True}
            self.hass.config_entries.async_update_entry(self._entry, data=nuovi_dati)

        self.pending_since = None
        self.pending_ticket = None
        self._pulisci_ticket_pendente()

        dati = await self._con_ultime_date(
            {
                "stato": "ok",
                "ultimo_aggiornamento": data_a.isoformat(),
                "periodo_importato": f"{data_da.isoformat()} - {data_a.isoformat()}",
                "pod_aggiornati": list(risultati.keys()),
                "backfill": is_backfill,
                "totale_kwh_periodo_per_pod": totali_kwh,
            }
        )
        self.async_set_updated_data(dati)
