"""Costanti per l'integrazione Duereti Letture."""

DOMAIN = "duereti_letture"

CONF_CLIENT_ID = "client_id"
CONF_SECRET_ID = "secret_id"
CONF_PODS = "pods"  # lista di dict: {"pod": "IT001...", "df": "RSSMRA..."} - df = dato fiscale (CF o P.IVA)
# Data (ISO) del giorno in cui l'integrazione è stata configurata. Al primo
# avvio non si richiedono dati: ci si limita a validare le credenziali, e le
# richieste giornaliere partono dal giorno successivo.
CONF_DATA_INSTALLAZIONE = "data_installazione"

# I dati di un giorno risultano disponibili il giorno successivo: verificato
# sul campo (richiesta del 3 agosto fatta la mattina del 4, file pronto alle
# 15 dello stesso 4 agosto).
RITARDO_DATI_GIORNI = 1

# Prima di quest'ora (locale) non si chiede nulla: i dati del giorno
# precedente non sono ancora pronti a inizio mattinata.
ORA_MINIMA_RICHIESTA = 10

# Ticket requestExport in sospeso, salvato sulla config entry (sopravvive ai
# reload/riavvii) così un reload non perde di vista un ticket già ottenuto
# da Duereti - altrimenti si rischia di rifare requestExport da capo mentre
# quello precedente sta ancora venendo processato lato loro.
CONF_PENDING_TICKET = "pending_ticket"
CONF_PENDING_DATA_DA = "pending_data_da"
CONF_PENDING_DATA_A = "pending_data_a"
CONF_PENDING_IS_BACKFILL = "pending_is_backfill"

BASE_URL = "https://areaclienti.duereti.it/ClientiDueRetiWeb/public/misure"
URL_REQUEST_TOKEN = f"{BASE_URL}/requestToken"
URL_REQUEST_EXPORT = f"{BASE_URL}/requestExport"
URL_REQUEST_RESULT = f"{BASE_URL}/requestResult"

# Il token dichiarato dal manuale è valido 10 minuti. Un margine troppo ampio
# fa scattare il 409 CONFLICT (richiesta di nuovo token mentre il precedente
# è ancora attivo lato server), quindi lo teniamo piccolo.
TOKEN_VALIDITY_SECONDS = 600
TOKEN_SAFETY_MARGIN_SECONDS = 15

# Retry per il conflitto 409 su requestToken quando non c'è un token in cache
# da riusare (tipicamente: riavvio/reload di HA entro i 10 minuti di validità
# di un token precedente). Attesa breve apposta per non far scattare il
# timeout di setup di Home Assistant.
RIAVVIO_RETRY_ATTEMPTS = 3
RIAVVIO_RETRY_WAIT_SECONDS = 20

MODE_CURVE = "CURVE"
# Non usata dall'integrazione (che importa solo le curve): resta come
# riferimento al protocollo, ed è la modalità gestita da parse_letture_zip.
MODE_LETTURE = "LETTURE"

# Polling di requestResult: il job è schedulato lato Duereti e può richiedere
# ore. Intervalli troppo ravvicinati non hanno senso e sprecano chiamate;
# usiamo 30 minuti tra un tentativo e l'altro. Essendo più lungo dei 10
# minuti di validità del token, ad ogni tentativo il token va comunque
# rinnovato (gestito automaticamente da _get_token in api.py).
RESULT_POLL_INTERVAL_SECONDS = 1800  # 30 minuti
RESULT_POLL_MAX_ATTEMPTS = 12  # ~6 ore totali di attesa massima

# I dati vengono probabilmente validati/chiusi a fine mese, non giorno per
# giorno: il coordinator richiede sempre l'intero mese precedente completo
# (vedi coordinator._mese_precedente_completo) e si auto-limita a non
# rifare la stessa richiesta più volte per lo stesso mese. Un controllo
# giornaliero va bene: se il mese è già stato importato, il coordinator
# non rifà comunque la chiamata.
DEFAULT_SCAN_INTERVAL_HOURS = 1

ESITO_OK = 0
# Non usata: il codice verifica esito != ESITO_OK. Documentata per
# completezza, il manuale definisce solo questi due valori.
ESITO_ERRORE = 1

# Limiti dichiarati dal manuale (sezione requestExport). Non imposti dal
# codice: l'integrazione fa una richiesta per volta, quindi il limite di
# concorrenza non è raggiungibile. Documentato per chi legge.
MAX_CONCURRENT_REQUESTS = 5
MAX_SUPPLY_POINTS_PER_REQUEST = 200
MAX_DATE_RANGE_MONTHS = 6

# URL del repository GitHub, mostrato nel popup di configurazione
GITHUB_REPO_URL = "https://github.com/riccardorossi92/HomeAssistant-Duereti"
