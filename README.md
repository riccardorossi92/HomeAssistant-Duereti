# HomeAssistant-Duereti

Unofficial integration for Duereti electricity distributor data in Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/)
[![GitHub Release](https://img.shields.io/github/v/release/riccardorossi92/HomeAssistant-Duereti.svg?style=for-the-badge&color=blue)](https://github.com/riccardorossi92/HomeAssistant-Duereti/releases)
[![Integration Usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&style=for-the-badge&logo=home-assistant&label=usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$['HomeAssistant-Duereti'].total)](https://analytics.home-assistant.io/)

> **Disclaimer:**  This is an unofficial integration and is not affiliated with or endorsed by Duereti in any way.
Custom integration for Home Assistant to import electricity meter curve data from Duereti (Italian electricity distributor) through their public PCF API.

Integrazione per Home Assistant che scarica automaticamente le curve di
scambio/produzione (o le letture) del tuo POD da Duereti tramite le API
pubbliche del Portale Clienti Finali (PCF), e le importa come statistiche
esterne (visibili anche nella Energy Dashboard).

> ✅ **Formato CURVE confermato.** `parse_curve_zip` (in `api.py`) è stato
> validato su un export reale (CSV `;`-separato con colonne `POD`, `DATA`,
> `ORA`, `ATTIVA_PRELEVATA`, ecc., timestamp a intervalli di 15 minuti,
> decimali con virgola). Il parser LETTURE (`parse_letture_zip`) è anch'esso
> confermato ma resta solo di riferimento, dato che l'integrazione usa CURVE.

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

- Ogni giorno controlla se il **mese precedente completo** è già stato
  richiesto; se non lo è ancora, lo richiede (le curve/misure vengono
  probabilmente validate e chiuse a fine mese, non giorno per giorno —
  chiedere un giorno del mese in corso rischia di far restare il job in
  coda a tempo indeterminato perché quei dati non esistono ancora)
- Importa i dati come **external statistics** (`duereti_letture:<pod>_energia`),
  consultabili in **Impostazioni → Statistiche del sistema** o nella
  Energy Dashboard
- Espone un sensore diagnostico con la data dell'ultimo import riuscito

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
