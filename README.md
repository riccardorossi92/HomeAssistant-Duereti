# HomeAssistant-Duereti

> **Disclaimer:**  This is an unofficial integration and is not affiliated with or endorsed by Duereti in any way.
Custom integration for Home Assistant to import electricity meter curve data from Duereti (Italian electricity distributor) through their public PCF API.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/)
[![GitHub Release](https://img.shields.io/github/v/release/riccardorossi92/HomeAssistant-Duereti.svg?style=for-the-badge&color=blue)](https://github.com/riccardorossi92/HomeAssistant-Duereti/releases)
[![Integration Usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&style=for-the-badge&logo=home-assistant&label=usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$['duereti_letture'].total)](https://analytics.home-assistant.io/)

Integrazione per Home Assistant che scarica automaticamente le curve di
consumo elettrico del tuo POD da Duereti tramite le API pubbliche del Portale
Clienti Finali (PCF), e le importa come statistiche esterne (visibili anche
nella Energy Dashboard).

## Prerequisiti: richiedere Client ID e Secret ID a Duereti

Le API non sono pubbliche in modo libero: vanno abilitate manualmente da
Duereti, che poi ti invia via email le credenziali (`client_id` e
`secret_id`) da usare in questa integrazione.

1. Accedi al **Portale Clienti Finali (PCF)** di Duereti:
   `https://areaclienti.duereti.it/ClientiDueRetiWeb`
2. Assicurati di avere **almeno un'identificazione validata** dal
   backoffice Duereti sul tuo profilo: senza questo passaggio la richiesta
   di abilitazione API non compare nemmeno.
3. Vai nella sezione **"Area POD/PDR: Interruzioni, Misure e servizi"** e
   cerca l'opzione per richiedere l'abilitazione all'uso delle API.
4. Invia la richiesta e attendi l'accettazione da parte di Duereti.
5. Una volta approvata, riceverai via email **Client ID** e **Secret ID**
   (sono comunque visibili anche nella stessa pagina del portale da cui hai
   fatto la richiesta).
6. Prendi nota anche di:
   - il/i **codice/i POD o PDR** che vuoi monitorare;
   - il **dato fiscale** associato a ciascun POD/PDR (codice fiscale o
     partita IVA a seconda dell'intestatario — richiesto da Duereti ad
     ogni chiamata insieme al POD).

Questo processo è interamente gestito da Duereti: l'integrazione non può
velocizzarlo né bypassarlo.

Limiti dichiarati dal manuale API: max 5 richieste in contemporanea, max
200 POD/PDR per richiesta, range di date massimo 6 mesi.

## Installazione

### Tramite HACS (custom repository)

1. HACS → menu (⋮) → **Repository personalizzate**
2. Aggiungi `https://github.com/riccardorossi92/HomeAssistant-Duereti`,
   categoria **Integrazione**
3. Installa "Duereti Letture" e riavvia Home Assistant

### Manuale

1. Copia la cartella `custom_components/duereti_letture` nella cartella
   `custom_components` della tua configurazione Home Assistant
2. Riavvia Home Assistant

## Configurazione

1. **Impostazioni → Dispositivi e Servizi → Aggiungi integrazione**, cerca
   **Duereti Letture**
2. Al primo passaggio compare il popup che chiede **Client ID** e
   **Secret ID** ottenuti da Duereti (vedi sopra) — vengono validati subito
   con una chiamata a `requestToken`
3. Al passaggio successivo aggiungi uno o più **POD** con il relativo
   **codice fiscale**; puoi aggiungerne quanti vuoi prima di confermare
4. Dopo la configurazione puoi aggiungere/rimuovere POD in qualsiasi
   momento da **Configura** sull'integrazione (Opzioni)

## Cosa fa una volta configurata

I dati di un giorno risultano disponibili presso Duereti il **giorno
successivo** (verificato sul campo: la richiesta del 3 agosto, inviata la
mattina del 4, è stata evasa alle 15 dello stesso giorno).

- **Al primo avvio viene richiesto subito il giorno precedente,** senza
  attendere le 10:00: serve a verificare da subito che POD e dato fiscale
  siano validi. Se non lo sono, l'errore emerge ora invece che il giorno dopo,
  e riguarda le entità del POD, non quelle dell'account.
- **Dal giorno successivo, dopo le 10:00,** viene richiesto il giorno
  precedente. Il controllo gira ogni ora, ma quasi sempre non fa nulla ed
  esce subito: serve solo a intercettare la finestra delle 10:00
  indipendentemente da quando Home Assistant è stato avviato.
- **Lo storico non viene recuperato automaticamente:** si richiede quando
  vuoi con l'azione `recupera_storico` (vedi sotto).

I dati vengono importati come **external statistics**
(`duereti_letture:<pod>_energia`), consultabili in **Impostazioni → Sistema →
Statistiche** e utilizzabili nella Energy Dashboard.

### Entità esposte

Tutte diagnostiche: i consumi veri stanno nelle statistiche, non in un
sensore. Sono raggruppate in un dispositivo "Account API" più un dispositivo
per ogni POD.

| Entità | Dispositivo | Cosa mostra |
|---|---|---|
| Ultimo import | Account | Fine del periodo dell'ultimo import riuscito |
| Attesa file (minuti) | Account | Da quanto è in corso il polling di `requestResult`; `0` se non c'è nulla in coda |
| POD configurati | Account | Quanti POD in questa istanza |
| Ultima data disponibile | POD | Ultimo giorno per cui esistono davvero dati importati |
| Consumo ultimo periodo | POD | kWh totali dell'ultimo periodo importato |

Il sensore *Attesa file* è utile per un'automazione di allerta: se resta alto
per ore, qualcosa si è inceppato.

Le entità dell'**Account API** restano disponibili anche quando il recupero
dei dati fallisce: è lì che si leggono gli attributi `stato` e
`ultimo_errore`, proprio quando servono. Vanno in errore solo se fallisce
l'autenticazione, e in quel caso Home Assistant propone il reinserimento
delle credenziali. Le entità del **POD** invece segnalano l'errore, perché
un problema di recupero riguarda i dati di quel punto di prelievo.

### Azione `duereti_letture.recupera_storico`

Richiede un periodo passato e lo importa. Ogni richiesta può restare in coda
per ore e le API accettano al massimo 6 mesi per volta, quindi sei tu a
decidere quando e quanto recuperare; per periodi più lunghi ripeti l'azione su
intervalli consecutivi.

```yaml
action: duereti_letture.recupera_storico
data:
  data_da: "2026-02-01"
  data_a: "2026-07-31"
```

Il periodo viene validato subito, con un errore comprensibile in interfaccia
invece di una richiesta che Duereti rifiuterebbe ore dopo: inizio successivo
alla fine, fine oltre ieri, o periodo superiore ai 6 mesi consentiti.

### Azione `duereti_letture.recupera_ticket`

Riprende un ticket già esistente e ne avvia direttamente il polling, saltando
`requestExport`. Serve quando hai ottenuto un ticket per altre vie (una
chiamata manuale con curl o Bruno) o vuoi riprenderne uno ancora valido,
evitando proprio la chiamata che il WAF blocca più spesso.

```yaml
action: duereti_letture.recupera_ticket
data:
  ticket: XXXXXXXXXXXXXXXXXXXXXX
  data_da: "2026-07-01"   # opzionale
  data_a: "2026-07-31"    # opzionale
```

Le date sono solo un'etichetta per i sensori diagnostici: i dati importati
arrivano interamente dal file. `entry_id` serve solo con più istanze
configurate.


## Comuni serviti da Duereti
<details>
<summary>Provincia di Milano</summary>
- Abbiategrasso
- Albairate
- Arconate
- Arese
- Arluno
- Assago
- Bareggio
- Basiglio
- Bellinzago Lombardo
- Bernate Ticino
- Besate
- Binasco
- Boffalora sopra Ticino
- Bubbiano
- Buccinasco
- Buscate
- Bussero
- Busto Garolfo
- Calvignasco
- Canegrate
- Carpiano
- Casarile
- Casorezzo
- Cassano d'Adda
- Cassina de' Pecchi
- Cassinetta di Lugagnano
- Castano Primo
- Cernusco sul Naviglio
- Cerro al Lambro
- Cerro Maggiore
- Cesano Boscone
- Cisliano
- Colturano
- Corbetta
- Cornaredo
- Corsico
- Cuggiono
- Cusago
- Dairago
- Dresano
- Gaggiano
- Garbagnate Milanese
- Gessate
- Gorgonzola
- Gudo Visconti
- Inveruno
- Inzago
- Lacchiarella
- Lainate
- Legnano
- Liscate
- Locate di Triulzi
- Magenta
- Magnago
- Marcallo con Casone
- Masate
- Mediglia
- Melegnano
- Melzo
- Mesero
- Morimondo
- Motta Visconti
- Nerviano
- Nosate
- Noviglio
- Opera
- Ossona
- Ozzero
- Pantigliate
- Parabiago
- Paullo
- Pero
- Peschiera Borromeo
- Pessano con Bornago
- Pieve Emanuele
- Pioltello
- Pogliano Milanese
- Pozzo d'Adda
- Pozzuolo Martesana
- Pregnana Milanese
- Rescaldina
- Rho
- Robecchetto con Induno
- Robecco sul Naviglio
- Rodano
- Rosate
- San Donato Milanese
- San Giorgio su Legnano
- San Giuliano Milanese
- San Vittore Olona
- San Zenone al Lambro
- Santo Stefano Ticino
- Sedriano
- Segrate
- Settala
- Settimo Milanese
- Trezzano sul Naviglio
- Tribiano
- Truccazzano
- Turbigo
- Vanzaghello
- Vanzago
- Vaprio d'Adda
- Vermezzo con Zelo
- Vernate
- Vignate
- Villa Cortese
- Vimodrone
- Vittuone
- Vizzolo Predabissi
- Zibido San Giacomo
</details>

<details>
<summary>Val Trompia</summary>
- Bovegno
- Bovezzo
- Brione
- Caino
- Collio
- Concesio
- Gardone Val Trompia
- Irma
- Lodrino
- Lumezzane
- Marcheno
- Marmentino
- Nave
- Pezzaze
- Polaveno
- Sarezzo
- Tavernole sul Mella
- Villa Carcina
</details>


## Note tecniche / limiti noti

- Il token Duereti dura 10 minuti: viene rinnovato automaticamente quando
  serve (non ad ogni chiamata, per evitare il 409 CONFLICT descritto sotto)
- `requestResult` fa polling ogni 30 minuti per un massimo di ~6 ore in
  attesa che il job schedulato da Duereti produca il file
- Il formato del contenuto dello zip è confermato: CSV `;`-separato con
  colonne `POD;DATA;ORA;FL_ORA_LEGALE;ATTIVA_PRELEVATA;ATTIVA_IMMESSA;...`,
  decimali con virgola, timestamp a intervalli di 15 minuti. Viene importato
  solo `ATTIVA_PRELEVATA` (consumo); `ATTIVA_IMMESSA` (produzione, es.
  fotovoltaico) non è ancora gestita
- L'integrazione usa solo `mode=CURVE`. Esiste anche un parser per le letture
  (`parse_letture_zip`), validato su un file reale ma non collegato
  all'integrazione: `mode=LETTURE` restituisce solo una fotografia cumulativa
  a fine periodo, non l'andamento nel tempo
- I quarti d'ora vengono **aggregati per ora**, perché le external statistics
  di Home Assistant sono orarie
- Ad ogni import la serie esistente viene riletta, fusa con i nuovi dati e le
  somme progressive ricalcolate da zero. Costa qualche migliaio di righe
  lette per volta, ma rende irrilevante l'ordine di inserimento: senza, non
  si potrebbero importare periodi anteriori ai dati già presenti, perché la
  somma è cumulativa e i punti successivi resterebbero incoerenti

### Il blocco intermittente del WAF

Le chiamate alle API vengono rifiutate a intermittenza da un WAF (F5 BIG-IP)
con una pagina HTML `Request Rejected` restituita con **HTTP 200**, al posto
del JSON atteso. Succede su tutte e tre le chiamate, indipendentemente da
rete e credenziali, e non è aggirabile lato client: è un problema lato
Duereti, per il quale è aperta una segnalazione.

L'integrazione è costruita per conviverci:

- un fallimento del recupero dati non fa fallire l'integrazione, solo il
  singolo aggiornamento; Home Assistant riprova da solo
- il ticket ottenuto viene salvato in modo persistente e ripreso dopo
  riavvii, reload o errori: viene scartato solo se Duereti lo dichiara
  esplicitamente non valido (404) o dopo un import riuscito
- un blocco durante il polling è trattato come transitorio, senza perdere il
  ticket

### Ora legale

La colonna `FL_ORA_LEGALE` viene usata per costruire timestamp con l'offset
UTC corretto, così nell'ora ripetuta di fine ottobre i due intervalli con lo
stesso orario locale restano distinti. **Attenzione:** la mappatura dei
valori (`2` = ora legale, `1` = ora solare) è un'inferenza — negli export
osservati finora, tutti in ora legale, il valore era sempre `2` — e non è
documentata nel manuale. Su un valore inatteso si ricade sul fuso locale con
un warning nei log.

### Diagnostica

Per vedere esattamente quali chiamate vengono fatte e cosa risponde Duereti:

```yaml
logger:
  default: warning
  logs:
    custom_components.duereti_letture: debug
```

Vengono loggati URL, header, body inviato e risposta grezza di ogni
chiamata. Credenziali e token sono oscurati, quindi i log si possono
condividere per chiedere supporto.

### Codici di errore documentati dal manuale

| Chiamata | HTTP | Significato |
|---|---|---|
| requestToken | 400 | Secret/ClientID mancanti |
| requestToken | 401 | Credenziali non valide |
| requestToken | 409 | Token già esistente per l'utente (restituito nel messaggio) |
| requestExport | 400 | Validazione fallita, oppure elaborazione già presente (ticket nel messaggio) |
| requestExport | 401 | Token errato o mancante |
| requestExport | 429 | Limite di richieste attive raggiunto |
| requestResult | 400 | Validazione fallita |
| requestResult | 401 | Token errato o mancante |
| requestResult | 404 | Ticket presente ma non collegato a nessun dato |

Il caso "file non ancora pronto" è invece un HTTP 200 con
`{"esito":1, "message":"Il file non è ancora disponibile"}` — non è un errore,
è lo stato normale mentre il job è in coda.

## Icona/logo

Per far comparire il logo Duereti in HACS e nell'interfaccia HA, metti i file
richiesti dentro `custom_components/duereti_letture/brand/` (vedi il
`README.md` in quella cartella per i dettagli). Funziona da Home Assistant
2026.3 in poi; su versioni precedenti l'integrazione funziona comunque, solo
senza icona personalizzata.

## Sviluppo e test

```bash
pip install -r requirements_test.txt --break-system-packages
pytest
```

`tests/test_api.py` copre il parsing dei file CSV/XLSX (formato confermato
con dati reali) e non richiede Home Assistant installato. Gli altri file di
test richiedono `pytest-homeassistant-custom-component` (incluso nei
requirements) perché importano moduli che dipendono da `homeassistant.*`.

## Riferimento API

Manuale ufficiale Duereti: *"Manuale d'uso – API per Estrazione Curve e
Letture"*, disponibile dal PCF nella stessa sezione da cui si richiede
l'abilitazione.

## Licenza

MIT — vedi [LICENSE](LICENSE).
