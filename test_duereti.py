#!/usr/bin/env python3
"""Script standalone per testare le API Duereti da terminale, senza Home Assistant.

Riusa la stessa logica di custom_components/duereti_letture/api.py ma senza
dipendenze da HA, cosi' puoi validare credenziali/POD e soprattutto ISPEZIONARE
il contenuto reale dello zip restituito, prima di finalizzare il parser
usato dall'integrazione.

Uso:
    pip install aiohttp
    export DUERETI_CLIENT_ID="..."
    export DUERETI_SECRET_ID="..."
    python test_duereti.py --pod IT001E12345678 --df RSSMRA80A01F205X \
        --data-da 2026-06-01 --data-a 2026-06-30 --mode CURVE

NOTA: i dati di un giorno risultano disponibili il giorno successivo
(verificato: richiesta del 3 agosto evasa il 4). Richiedere il giorno corrente
lascia il job in coda perché quei dati non esistono ancora. Il range massimo
per richiesta è di 6 mesi.

Il file risultato viene salvato in ./output/ sia come .zip grezzo che come
elenco/estratto dei file interni, cosi' puoi confrontare i nomi colonna reali
con quelli attesi in parse_curve_zip() dell'integrazione.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import io
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import aiohttp

BASE_URL = "https://areaclienti.duereti.it/ClientiDueRetiWeb/public/misure"
URL_REQUEST_TOKEN = f"{BASE_URL}/requestToken"
URL_REQUEST_EXPORT = f"{BASE_URL}/requestExport"
URL_REQUEST_RESULT = f"{BASE_URL}/requestResult"

RESULT_POLL_INTERVAL_SECONDS = 1800  # 30 minuti, come nell'integrazione
RESULT_POLL_MAX_ATTEMPTS = 12  # ~6 ore totali

OUTPUT_DIR = Path("output")


async def json_or_raise(resp: aiohttp.ClientResponse) -> dict:
    """Il manuale conferma che anche le risposte 4xx hanno body JSON con
    esito/message: lo leggiamo sempre, e segnaliamo lo status per capire
    a quale scenario documentato corrisponde."""
    import json

    text = await resp.text()
    try:
        data = json.loads(text)
    except Exception as err:  # noqa: BLE001
        print(f"   [debug] status={resp.status} headers={dict(resp.headers)}")
        raise RuntimeError(f"Risposta non JSON (HTTP {resp.status}): {text[:500]}") from err

    if resp.status != 200:
        print(f"   [debug] HTTP {resp.status}: {data}")

    return data, resp.status


async def request_token(session: aiohttp.ClientSession, client_id: str, secret_id: str) -> str:
    print("-> requestToken")
    body = {"clientId": client_id, "secretId": secret_id}
    print(f"   [debug] body inviato: {body}")
    async with session.post(URL_REQUEST_TOKEN, json=body) as resp:
        print(f"   [debug] request headers: {dict(resp.request_info.headers)}")
        data, status = await json_or_raise(resp)

    if status == 400:
        raise RuntimeError(f"400 BAD_REQUEST: Secret/ClientID mancanti ({data.get('message')})")
    if status == 401:
        raise RuntimeError(f"401 UNAUTHORIZED: credenziali non valide ({data.get('message')})")
    if status == 409:
        raise RuntimeError(
            f"409 CONFLICT: token già esistente per l'utente, dovrebbe essere nel messaggio: "
            f"{data.get('message')}"
        )
    if status != 200:
        raise RuntimeError(f"HTTP {status} inatteso: {data}")

    print(f"   risposta grezza: {data}")
    if data.get("esito") != 0:
        raise RuntimeError(f"Autenticazione fallita: {data.get('message')}")
    token = data["token"]
    print(f"   token ottenuto: {token[:6]}...")
    return token


async def request_export(
    session: aiohttp.ClientSession,
    token: str,
    data_da: str,
    data_a: str,
    mode: str,
    pod: str,
    df: str,
) -> str:
    print("-> requestExport")
    body = {
        "dataDa": data_da,
        "dataA": data_a,
        "mode": mode,
        "supplyPoints": [{"supplyPoint": pod, "df": df}],
    }
    headers = {"Authorization": token}
    async with session.post(URL_REQUEST_EXPORT, json=body, headers=headers) as resp:
        data, status = await json_or_raise(resp)

    if status == 401:
        raise RuntimeError(f"401 UNAUTHORIZED: token errato o mancante ({data.get('message')})")
    if status == 429:
        raise RuntimeError(f"429: limite richieste attive raggiunto ({data.get('message')})")
    if status == 400:
        # Puo' essere validazione fallita, oppure elaborazione già presente
        # con il ticket collegato comunicato nel messaggio.
        print(f"   400 BAD_REQUEST: {data.get('message')}")
        raise RuntimeError(f"requestExport fallita (400): {data.get('message')}")
    if status != 200:
        raise RuntimeError(f"HTTP {status} inatteso: {data}")

    print(f"   risposta grezza: {data}")
    if data.get("esito") != 0:
        raise RuntimeError(f"requestExport fallita: {data.get('message')}")
    ticket = data["ticket"]
    print(f"   ticket ottenuto: {ticket}")
    return ticket


async def request_result(
    session: aiohttp.ClientSession, client_id: str, secret_id: str, ticket: str
) -> tuple[dict, bytes | None]:
    print("-> requestResult (polling, fino a diverse ore)")
    body = {"ticket": ticket}

    token = await request_token(session, client_id, secret_id)
    token_timestamp = asyncio.get_event_loop().time()
    TOKEN_LIFETIME = 600  # secondi, come dichiarato dal manuale
    TOKEN_MARGIN = 15  # piccolo margine, per non chiedere un token nuovo troppo presto e beccare 409

    for attempt in range(1, RESULT_POLL_MAX_ATTEMPTS + 1):
        now = asyncio.get_event_loop().time()
        if now - token_timestamp > TOKEN_LIFETIME - TOKEN_MARGIN:
            token = await request_token(session, client_id, secret_id)
            token_timestamp = now

        headers = {"Authorization": token}
        async with session.post(URL_REQUEST_RESULT, json=body, headers=headers) as resp:
            data, status = await json_or_raise(resp)

        if status == 401:
            # Il token risulta comunque non valido: forziamo un rinnovo e
            # ritentiamo subito, senza aspettare l'intervallo pieno.
            print("   401 UNAUTHORIZED nonostante il token in cache: rinnovo e ritento subito")
            token = await request_token(session, client_id, secret_id)
            token_timestamp = asyncio.get_event_loop().time()
            continue
        if status == 429:
            print(f"   429: limite richieste attive raggiunto ({data.get('message')}), attendo di più")
            await asyncio.sleep(RESULT_POLL_INTERVAL_SECONDS * 2)
            continue
        if status == 404:
            raise RuntimeError(f"404 NOT_FOUND: ticket non collegato a nessun dato ({data.get('message')})")
        if status == 400:
            raise RuntimeError(f"400 BAD_REQUEST: {data.get('message')}")
        if status != 200:
            raise RuntimeError(f"HTTP {status} inatteso: {data}")

        # Stampiamo SEMPRE la risposta grezza (tranne il campo File se enorme)
        preview = dict(data)
        if isinstance(preview.get("File"), str) and len(preview["File"]) > 200:
            preview["File"] = preview["File"][:200] + f"...[{len(data['File'])} caratteri totali]"
        print(f"   tentativo {attempt}/{RESULT_POLL_MAX_ATTEMPTS}: {preview}")

        if data.get("esito") == 0 and data.get("File"):
            # Confermato dal manuale: File è sempre Base64 dello zip
            # (.csv per CURVE, .xlsx per LETTURE)
            try:
                return data, base64.b64decode(data["File"])
            except Exception as err:  # noqa: BLE001
                print(f"   ATTENZIONE: impossibile decodificare File come base64: {err}")
                return data, None

        if data.get("esito") == 1:
            # Confermato: {"esito":1, "message":"Il file non è ancora disponibile"}
            # e' lo stato normale mentre il job e' in coda, non un errore.
            print("   job ancora in coda, continuo il polling")

        await asyncio.sleep(RESULT_POLL_INTERVAL_SECONDS)

    raise RuntimeError(f"File non disponibile dopo {RESULT_POLL_MAX_ATTEMPTS} tentativi")


def ispeziona_zip(zip_bytes: bytes) -> None:
    print("\n=== CONTENUTO ZIP ===")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            print(f"\n--- file: {name} ---")
            with zf.open(name) as f:
                raw = f.read()
            out_path = OUTPUT_DIR / name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw)
            print(f"   salvato in: {out_path}")

            if name.lower().endswith((".csv", ".txt")):
                text = raw.decode("utf-8-sig", errors="replace")
                righe = text.splitlines()
                print(f"   prime righe ({min(5, len(righe))}/{len(righe)}):")
                for riga in righe[:5]:
                    print(f"     {riga}")
            elif name.lower().endswith(".xlsx"):
                print(f"   ({len(raw)} byte, file Excel - apri {out_path} per ispezionarlo)")
            else:
                print(f"   ({len(raw)} byte, non testuale/non anteprima)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test standalone API Duereti")
    parser.add_argument("--client-id", default=os.environ.get("DUERETI_CLIENT_ID"))
    parser.add_argument("--secret-id", default=os.environ.get("DUERETI_SECRET_ID"))
    parser.add_argument("--pod", required=True, help="Codice POD/PDR da interrogare")
    parser.add_argument("--df", required=True, help="Dato fiscale associato al POD/PDR (CF o P.IVA)")
    parser.add_argument("--data-da", required=True, help="Formato yyyy-mm-dd (es. 2026-07-20)")
    parser.add_argument("--data-a", required=True, help="Formato yyyy-mm-dd (es. 2026-07-20)")
    parser.add_argument("--mode", default="CURVE", choices=["CURVE", "LETTURE"])
    args = parser.parse_args()

    if not args.client_id or not args.secret_id:
        print("ERRORE: servono --client-id/--secret-id oppure le env var DUERETI_CLIENT_ID / DUERETI_SECRET_ID")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)

    async with aiohttp.ClientSession() as session:
        token = await request_token(session, args.client_id, args.secret_id)
        ticket = await request_export(
            session, token, args.data_da, args.data_a, args.mode, args.pod, args.df
        )
        _, zip_bytes = await request_result(session, args.client_id, args.secret_id, ticket)

    if zip_bytes is None:
        print("\nNessun file utilizzabile ottenuto, vedi i log sopra.")
        return

    zip_path = OUTPUT_DIR / f"duereti_{args.pod}_{args.data_da}_{args.data_a}.zip"
    zip_path.write_bytes(zip_bytes)
    print(f"\nZip grezzo salvato in: {zip_path}")

    ispeziona_zip(zip_bytes)


if __name__ == "__main__":
    asyncio.run(main())
