"""DataUpdateCoordinator per l'integrazione Duereti Letture."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DuretiApiClient,
    DuretiApiError,
    DuretiAuthError,
    DuretiNotFoundError,
    descrivi_errore,
)
from .const import (
    CONF_DATA_INSTALLAZIONE,
    CONF_PENDING_DATA_A,
    CONF_PENDING_DATA_DA,
    CONF_PENDING_IS_BACKFILL,
    CONF_PENDING_TICKET,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_DATE_RANGE_MONTHS,
    MINUTI_ATTESA_SUGGERITI,
    MODE_CURVE,
    ORA_MINIMA_RICHIESTA,
    RITARDO_DATI_GIORNI,
)
from .statistics import async_get_ultima_data_disponibile, async_import_curva

_LOGGER = logging.getLogger(__name__)

# Fasi della pianificazione, vedi DuretiCoordinator._prossima_richiesta
FASE_GIORNALIERO = "giornaliero"
# Recupero storico avviato a mano con l'azione recupera_storico.
FASE_STORICO = "storico"
# Ticket forzato dall'utente con l'azione recupera_ticket: importa i dati ma
# non fa avanzare lo stato delle fasi automatiche.
FASE_MANUALE = "manuale"


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


def _inizio_n_mesi_prima(fine: date, n_mesi: int) -> date:
    """Primo giorno del mese che inizia n_mesi (incluso il mese di 'fine') prima di 'fine'.

    Es. con fine=2026-07-31 e n_mesi=6 -> 2026-02-01 (Feb...Lug = 6 mesi).
    Con n_mesi=1 -> 2026-07-01, cioè lo stesso range della richiesta mensile
    normale: è il caso limite a cui converge il fallback a range decrescente.
    """
    anno, mese = fine.year, fine.month - (n_mesi - 1)
    while mese <= 0:
        mese += 12
        anno -= 1
    return date(anno, mese, 1)


class DuretiCoordinator(DataUpdateCoordinator):
    """Coordina il download mensile delle curve e il loro import come statistiche.

    La pianificazione ha tre fasi (dettagli in _prossima_richiesta):
    import iniziale del mese corrente fino a oggi-2, poi recupero storico a
    ritroso in blocchi da 6 mesi, infine richiesta giornaliera del solo
    giorno oggi-2.

    requestExport è rapida e resta nel ciclo normale del coordinator.
    requestResult può invece restare in coda per ore (polling ogni ~30 minuti,
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
        # True finché requestToken funziona. Distingue un problema di
        # account da un problema di recupero dati: le entità del
        # dispositivo "Account API" si basano su questo.
        self.token_ok: bool = True

        # Se la entry viene smontata (reload, riavvio, rimozione) mentre il
        # polling in background è a metà, lo cancelliamo invece di lasciarlo
        # orfano. Non perdiamo nulla: il ticket è già persistito sulla entry
        # (vedi _async_update_data), quindi la prossima istanza del
        # coordinator lo riprende comunque in modo pulito.
        entry.async_on_unload(self._annulla_task_in_background)

    @callback
    def _annulla_task_in_background(self) -> None:
        """Cancella il polling in corso quando la entry viene smontata.

        Sicuro da fare: il ticket è già persistito su self._entry.data prima
        che questo task venisse creato, quindi non si perde nulla - una
        nuova istanza del coordinator lo riprenderà al prossimo setup.
        """
        if self._background_task and not self._background_task.done():
            _LOGGER.debug(
                "Smontaggio della entry: annullo il polling in background ancora in corso"
            )
            self._background_task.cancel()

    async def async_forza_ticket(
        self, ticket: str, data_da: date | None = None, data_a: date | None = None
    ) -> None:
        """Riprende manualmente un ticket noto, saltando requestExport.

        Serve quando si è ottenuto un ticket per altre vie (es. una chiamata
        fatta a mano con curl/Bruno, o un ticket che l'integrazione aveva
        perso) e lo si vuole far elaborare senza chiedere a Duereti un nuovo
        export - operazione che il WAF blocca spesso.

        Se le date non vengono indicate si assume il mese precedente completo:
        servono solo come etichetta del periodo nei sensori diagnostici, non
        influenzano i dati, che arrivano interamente dal file.
        """
        if self._background_task and not self._background_task.done():
            # Forzare un ticket è un'azione deliberata dell'utente: ha la
            # precedenza sul recupero in corso, che viene annullato. Logghiamo
            # il ticket interrotto così resta recuperabile dai log (potrebbe
            # essere ancora valido e riutilizzabile in seguito).
            _LOGGER.warning(
                "Annullo il recupero in corso (ticket %s) per forzare il ticket %s",
                self.pending_ticket or "sconosciuto",
                ticket,
            )
            self._background_task.cancel()

        if data_da is None or data_a is None:
            default_da, default_a = _mese_precedente_completo(date.today())
            data_da = data_da or default_da
            data_a = data_a or default_a

        _LOGGER.info(
            "Ticket forzato manualmente: %s (periodo %s - %s)", ticket, data_da, data_a
        )

        nuovi_dati = {
            **self._entry.data,
            CONF_PENDING_TICKET: ticket,
            CONF_PENDING_DATA_DA: data_da.isoformat(),
            CONF_PENDING_DATA_A: data_a.isoformat(),
            CONF_PENDING_IS_BACKFILL: False,
        }
        self.hass.config_entries.async_update_entry(self._entry, data=nuovi_dati)

        self.pending_since = dt_util.utcnow()
        self.pending_ticket = ticket
        self._ultima_richiesta = None  # non è una richiesta nostra: non marcare il periodo
        self._background_task = self.hass.async_create_background_task(
            self._poll_and_import(ticket, data_da, data_a, fase=FASE_MANUALE),
            name=f"{DOMAIN}_poll_import_forzato_{ticket}",
        )

    async def async_recupera_storico(self, data_da: date, data_a: date) -> None:
        """Avvia manualmente il recupero di un periodo storico.

        Il recupero dello storico non è automatico: le API Duereti accettano
        al massimo 6 mesi per richiesta e ogni richiesta può restare in coda
        per ore, quindi è l'utente a decidere quando e quanto recuperare.

        Solleva ServiceValidationError se il periodo non è valido: meglio un
        errore immediato e comprensibile in interfaccia che una richiesta che
        Duereti rifiuterebbe ore dopo.
        """
        if data_da > data_a:
            raise ServiceValidationError(
                f"La data di inizio ({data_da}) è successiva a quella di fine ({data_a})."
            )

        ultimo_utile = date.today() - timedelta(days=RITARDO_DATI_GIORNI)
        if data_a > ultimo_utile:
            raise ServiceValidationError(
                f"La data di fine ({data_a}) è troppo recente: i dati sono disponibili con "
                f"un giorno di ritardo, quindi al massimo fino al {ultimo_utile}."
            )

        limite = _inizio_n_mesi_prima(data_a, MAX_DATE_RANGE_MONTHS)
        if data_da < limite:
            raise ServiceValidationError(
                f"Il periodo richiesto supera il limite di {MAX_DATE_RANGE_MONTHS} mesi "
                f"imposto dalle API Duereti. Con fine {data_a} l'inizio non può essere "
                f"anteriore al {limite}. Per recuperare più storico ripeti l'azione su "
                f"periodi consecutivi."
            )

        if self._background_task and not self._background_task.done():
            _LOGGER.warning(
                "Annullo il recupero in corso (ticket %s) per avviare lo storico %s - %s",
                self.pending_ticket or "sconosciuto",
                data_da,
                data_a,
            )
            self._background_task.cancel()

        _LOGGER.info("Recupero storico avviato manualmente: %s - %s", data_da, data_a)

        # Le due chiamate vengono fatte separatamente per poter distinguere il
        # tipo di problema nel messaggio d'errore: un blocco su requestToken
        # riguarda l'accesso in generale (conviene aspettare), uno su
        # requestExport riguarda questa specifica richiesta (conviene cambiare
        # il periodo). Nessun tentativo automatico: l'azione è manuale, decide
        # l'utente se e quando riprovare.
        try:
            await self.api.async_assicura_token()
        except DuretiAuthError as err:
            self.token_ok = False
            raise HomeAssistantError(
                f"Credenziali non valide: {descrivi_errore(err)}"
            ) from err
        except DuretiApiError as err:
            self.token_ok = False
            raise HomeAssistantError(
                f"Non è stato possibile autenticarsi su Duereti: {descrivi_errore(err)}. "
                f"Riprova tra {MINUTI_ATTESA_SUGGERITI} minuti."
            ) from err
        else:
            self.token_ok = True

        try:
            ticket = await self.api.request_export(data_da, data_a, self._pods, mode=MODE_CURVE)
        except DuretiApiError as err:
            raise HomeAssistantError(
                f"Duereti non ha accettato la richiesta per il periodo {data_da} - {data_a}: "
                f"{descrivi_errore(err)}. L'autenticazione funziona, quindi il problema "
                "riguarda questa richiesta: riprova cambiando le date."
            ) from err

        self.pending_since = dt_util.utcnow()
        self.pending_ticket = ticket
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_PENDING_TICKET: ticket,
                CONF_PENDING_DATA_DA: data_da.isoformat(),
                CONF_PENDING_DATA_A: data_a.isoformat(),
                CONF_PENDING_IS_BACKFILL: FASE_STORICO,
            },
        )
        self._background_task = self.hass.async_create_background_task(
            self._poll_and_import(ticket, data_da, data_a, fase=FASE_STORICO),
            name=f"{DOMAIN}_recupero_storico_{data_da}_{data_a}",
        )

    def _fase_da_entry(self) -> str:
        """Legge la fase del ticket pendente dalla config entry.

        Retrocompatibile con le versioni che salvavano un booleano o nomi di
        fasi non più esistenti: in dubbio si ricade su giornaliero.
        """
        valore = self._entry.data.get(CONF_PENDING_IS_BACKFILL)
        if isinstance(valore, str) and valore in (FASE_GIORNALIERO, FASE_STORICO, FASE_MANUALE):
            return valore
        return FASE_GIORNALIERO

    def _prossima_richiesta(self) -> tuple[str | None, date, date]:
        """Decide se c'è qualcosa da chiedere a Duereti in questo ciclo.

        - Al primo avvio dopo l'installazione si chiede subito il giorno
          precedente, senza attendere ORA_MINIMA_RICHIESTA: serve a verificare
          da subito che POD e dato fiscale siano validi, perché un errore
          emergerebbe altrimenti solo il giorno dopo.
        - Dai giorni successivi, e solo dopo ORA_MINIMA_RICHIESTA, si chiede
          il giorno oggi-RITARDO_DATI_GIORNI.

        Il recupero dello storico non è automatico: si avvia a mano con
        l'azione duereti_letture.recupera_storico.

        Restituisce (fase, data_da, data_a); fase è None se non c'è nulla da
        chiedere adesso.
        """
        oggi = date.today()
        installazione = self._entry.data.get(CONF_DATA_INSTALLAZIONE)

        giorno = oggi - timedelta(days=RITARDO_DATI_GIORNI)

        if not installazione:
            # Primo avvio: chiediamo subito il giorno precedente, senza
            # aspettare le 10:00 né il giorno dopo. Serve a verificare da
            # subito che POD e dato fiscale siano corretti: se non lo sono,
            # requestExport risponde con un errore di validazione e il
            # problema emerge ora invece che il giorno seguente.
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, CONF_DATA_INSTALLAZIONE: oggi.isoformat()},
            )
            _LOGGER.info(
                "Primo avvio: richiedo i dati del %s per verificare POD e dato fiscale. "
                "Da domani le richieste partiranno dopo le %d:00; per lo storico usa "
                "l'azione duereti_letture.recupera_storico.",
                giorno,
                ORA_MINIMA_RICHIESTA,
            )
            return FASE_GIORNALIERO, giorno, giorno

        adesso = dt_util.now()
        if adesso.hour < ORA_MINIMA_RICHIESTA:
            _LOGGER.debug(
                "Sono le %02d:%02d, attendo le %d:00 prima di chiedere i dati di ieri",
                adesso.hour,
                adesso.minute,
                ORA_MINIMA_RICHIESTA,
            )
            return None, oggi, oggi

        return FASE_GIORNALIERO, giorno, giorno

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
            fase = self._fase_da_entry()

            self.pending_since = dt_util.utcnow()
            self.pending_ticket = ticket_pendente
            self._background_task = self.hass.async_create_background_task(
                self._poll_and_import(ticket_pendente, data_da, data_a, fase),
                name=f"{DOMAIN}_poll_import_ripreso_{ticket_pendente}",
            )
            return await self._con_ultime_date(
                {
                    "stato": "ticket precedente ripreso, in attesa del file",
                    "ticket": ticket_pendente,
                    "periodo": f"{data_da.isoformat()} - {data_a.isoformat()}",
                    "fase": fase,
                }
            )

        fase, data_da, data_a = self._prossima_richiesta()
        if fase is None:
            return await self._con_ultime_date(
                self.data or {"stato": "nessuna richiesta necessaria al momento"}
            )

        chiave = f"{fase}:{data_da.isoformat()}_{data_a.isoformat()}"

        if chiave == self._ultima_richiesta:
            _LOGGER.debug(
                "Periodo %s già richiesto in questa sessione, nessuna nuova richiesta", chiave
            )
            return await self._con_ultime_date(
                self.data or {"stato": f"periodo {chiave} già richiesto"}
            )

        if await self._periodo_gia_coperto(data_a):
            _LOGGER.debug(
                "Periodo %s già coperto dalle statistiche esistenti: nessuna richiesta", chiave
            )
            self._ultima_richiesta = chiave
            return await self._con_ultime_date(
                self.data or {"stato": f"periodo {chiave} già coperto dai dati esistenti"}
            )

        # Prima la chiamata di autenticazione: il suo esito determina lo stato
        # del dispositivo "Account API", distinto da quello dei POD.
        try:
            await self.api.async_assicura_token()
        except DuretiAuthError as err:
            self.token_ok = False
            raise ConfigEntryAuthFailed(f"Credenziali non valide: {err}") from err
        except DuretiApiError as err:
            self.token_ok = False
            raise UpdateFailed(f"Errore chiamando requestToken: {err}") from err
        else:
            self.token_ok = True

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
            CONF_PENDING_IS_BACKFILL: fase,
        }
        self.hass.config_entries.async_update_entry(self._entry, data=nuovi_dati)

        self._background_task = self.hass.async_create_background_task(
            self._poll_and_import(ticket, data_da, data_a, fase),
            name=f"{DOMAIN}_poll_import_{chiave}",
        )

        return await self._con_ultime_date(
            {
                "stato": "richiesta inviata, in attesa del file",
                "ticket": ticket,
                "periodo": f"{data_da.isoformat()} - {data_a.isoformat()}",
                "fase": fase,
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
        delle external statistics, indipendentemente da nuove richieste.

        Se per un POD il database non restituisce nulla ma avevamo già un
        valore noto, quest'ultimo viene conservato: la scrittura delle
        statistiche passa dal recorder in modo asincrono, quindi una lettura
        può temporaneamente non vedere dati appena importati e non deve
        riportare il sensore a "Sconosciuto".
        """
        note = (self.data or {}).get("ultime_date_per_pod", {})
        ultime_date = dict(note)
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
        self, ticket: str, data_da: date, data_a: date, fase: str
    ) -> None:
        """Task in background: aspetta il file (anche per ore) e importa i dati."""
        try:
            zip_bytes = await self.api.request_result(ticket)
        except DuretiAuthError as err:
            # Le credenziali non c'entrano con la validità del ticket:
            # lo conserviamo, così dopo il reauth si riprende da lì.
            _LOGGER.error(
                "Credenziali non valide durante il polling: %s. Il ticket %s viene conservato "
                "e verrà ripreso dopo il reinserimento delle credenziali.",
                err,
                ticket,
            )
            self.pending_since = None
            self.pending_ticket = None
            self._avvia_reauth()
            dati = await self._con_ultime_date(
                {
                    **(self.data or {}),
                    "stato": f"credenziali non valide: {err}",
                    "ultimo_errore": str(err),
                    "ticket_conservato": ticket,
                }
            )
            self.async_set_updated_data(dati)
            return
        except DuretiNotFoundError as err:
            # Unico caso in cui ha senso scartare il ticket: Duereti dice
            # esplicitamente che non è collegato a nessun dato.
            _LOGGER.error("Ticket %s non valido lato Duereti: %s", ticket, err)
            self.pending_since = None
            self.pending_ticket = None
            self._pulisci_ticket_pendente()
            dati = await self._con_ultime_date(
                {**(self.data or {}), "stato": f"ticket non valido: {err}", "ultimo_errore": str(err)}
            )
            self.async_set_updated_data(dati)
            return
        except DuretiApiError as err:
            # Timeout del polling, blocco WAF, errore di rete: il ticket lato
            # Duereti resta valido, quindi lo CONSERVIAMO e al prossimo ciclo
            # riprendiamo da lì invece di richiederne uno nuovo.
            _LOGGER.error(
                "Errore recuperando il file per il ticket %s: %s. Il ticket viene conservato "
                "per riprovare al prossimo ciclo.",
                ticket,
                err,
            )
            self.pending_since = None
            self.pending_ticket = None
            dati = await self._con_ultime_date(
                {
                    **(self.data or {}),
                    "stato": f"errore: {err}",
                    "ultimo_errore": str(err),
                    "ticket_conservato": ticket,
                }
            )
            self.async_set_updated_data(dati)
            return

        # Da qui in poi il file è stato ricevuto: qualunque errore in
        # decodifica/parsing/import va gestito, altrimenti l'eccezione esce dal
        # task in background lasciando pending_since valorizzato per sempre
        # (il sensore "Attesa file" continuerebbe a salire all'infinito pur
        # avendo già ricevuto i dati) e il ticket persistito non ripulito.
        try:
            from .api import parse_curve_zip

            _LOGGER.debug(
                "File ricevuto per il ticket %s (%d byte), avvio parsing", ticket, len(zip_bytes)
            )
            risultati = parse_curve_zip(zip_bytes)
            _LOGGER.debug(
                "Parsing completato: %d POD trovati nel file (%s)",
                len(risultati),
                ", ".join(f"{pod}: {len(r.punti)} punti" for pod, r in risultati.items()) or "nessuno",
            )

            totali_kwh = {
                pod: round(sum(p.valore_kwh for p in ris.punti), 3) for pod, ris in risultati.items()
            }

            date_importate: dict[str, str] = {}
            for pod_conf in self._pods:
                pod = pod_conf["pod"]
                risultato = risultati.get(pod)
                if risultato is None:
                    _LOGGER.warning(
                        "Nessun dato ricevuto per POD %s (periodo %s - %s). POD presenti nel "
                        "file: %s",
                        pod,
                        data_da,
                        data_a,
                        list(risultati.keys()) or "nessuno",
                    )
                    continue
                _LOGGER.debug("Importo %d punti per il POD %s", len(risultato.punti), pod)
                ultima = await async_import_curva(self.hass, pod, risultato)
                if ultima is not None:
                    date_importate[pod] = ultima.isoformat()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "Errore elaborando il file ricevuto per il ticket %s: %s. Il ticket viene "
                "CONSERVATO: il file lato Duereti è valido, il problema è nell'elaborazione "
                "locale, quindi al prossimo ciclo verrà riscaricato con lo stesso ticket "
                "invece di sprecarne uno nuovo (che il WAF potrebbe bloccare).",
                ticket,
                err,
            )
            # Volutamente NON chiamiamo _pulisci_ticket_pendente(): un errore
            # qui riguarda il nostro codice (parsing, import, formato dei dati),
            # non la validità del ticket. Ripulirlo significherebbe buttare via
            # un ticket funzionante e doverne richiedere un altro, cosa tutt'altro
            # che gratuita visti i blocchi intermittenti del WAF su requestExport.
            self.pending_since = None
            self.pending_ticket = None
            dati = await self._con_ultime_date(
                {
                    **(self.data or {}),
                    "stato": f"errore elaborando il file: {err}",
                    "ultimo_errore": str(err),
                    "ticket_conservato": ticket,
                }
            )
            self.async_set_updated_data(dati)
            return

        self.pending_since = None
        self.pending_ticket = None
        self._pulisci_ticket_pendente()

        dati = await self._con_ultime_date(
            {
                "stato": "ok",
                "ultimo_aggiornamento": data_a.isoformat(),
                "periodo_importato": f"{data_da.isoformat()} - {data_a.isoformat()}",
                "pod_aggiornati": list(risultati.keys()),
                "fase": fase,
                "totale_kwh_periodo_per_pod": totali_kwh,
            }
        )
        # Le date appena importate hanno la precedenza su quelle rilette dal
        # database: async_add_external_statistics accoda la scrittura al
        # recorder, quindi una rilettura immediata non le vedrebbe ancora e il
        # sensore "Ultima data disponibile" resterebbe a "Sconosciuto" fino al
        # ciclo successivo (24 ore dopo).
        dati["ultime_date_per_pod"] = {
            **dati.get("ultime_date_per_pod", {}),
            **date_importate,
        }
        self.async_set_updated_data(dati)
