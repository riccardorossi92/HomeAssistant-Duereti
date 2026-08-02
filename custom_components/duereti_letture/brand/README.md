# Icone del brand

Metti qui i file del logo Duereti per farli comparire in HACS e nell'interfaccia
di Home Assistant (Impostazioni → Dispositivi e Servizi, e dentro HACS stesso).

Da Home Assistant 2026.3 in poi, HA serve queste immagini direttamente da qui
tramite l'API locale `/api/brands/integration/duereti_letture/icon.png` — non
serve più fare una PR al repository home-assistant/brands.

## File richiesti

| File | Uso | Requisiti |
|---|---|---|
| `icon.png` | Icona quadrata, quella che vedi nell'elenco integrazioni | 1:1, almeno 128px, non oltre 256px |
| `icon@2x.png` | Versione hDPI dell'icona | 1:1, almeno 256px, non oltre 512px |
| `logo.png` | Logo esteso (opzionale, mostrato in alcune schermate) | formato landscape, rispetta le proporzioni del logo reale |
| `logo@2x.png` | Versione hDPI del logo (opzionale) | come sopra, doppia risoluzione |

## Requisiti tecnici comuni

- Formato **PNG**
- **Sfondo trasparente** (non bianco/colorato)
- Nessun testo scritto a mano sopra il logo, nessuna modifica rispetto all'originale
- Non usare loghi/immagini di Home Assistant (per non generare confusione con
  un'integrazione ufficiale)

## Dove prendere il logo Duereti

Scaricalo dal sito ufficiale di Duereti (es. dalla loro pagina "chi siamo" o
dal footer del sito, spesso c'è una versione in alta risoluzione), oppure
chiedilo direttamente a Duereti se non lo trovi in un formato adatto.
