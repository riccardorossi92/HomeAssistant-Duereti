"""Test per api.py: parsing dei file CSV/XLSX restituiti da Duereti.

Questi test NON dipendono da Home Assistant (api.py importa solo aiohttp e
libreria standard), quindi girano con un semplice `pytest tests/test_api.py`.
"""
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "duereti_letture"))

from api import (  # noqa: E402
    DuretiApiError,
    DuretiAuthError,
    parse_curve_zip,
    parse_letture_zip,
)

CSV_ESEMPIO = (
    "POD;DATA;ORA;FL_ORA_LEGALE;ATTIVA_PRELEVATA;ATTIVA_IMMESSA;"
    "REATTIVA_CAPACITIVA_IMMESSA;REATTIVA_CAPACITIVA_PRELEVATA;"
    "REATTIVA_INDUTTIVA_IMMESSA;REATTIVA_INDUTTIVA_PRELEVATA;"
    "PICCO_PRELEVATA;CONSUMO_PICCO_IMMESSA;TIPO_DATO;\n"
    "IT001E00000001;20260501;000000;2;0,025;;;;;0,001;5,004;;E;\n"
    "IT001E00000001;20260501;001500;2;0,028;;;;;0,0;5,004;;E;\n"
    "IT001E00000001;20260501;003000;2;0,037;;;;;0,0;5,004;;E;\n"
)


def _zip_da_csv(nome_file: str, contenuto: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(nome_file, contenuto)
    return buf.getvalue()


def test_parse_curve_zip_righe_valide():
    zip_bytes = _zip_da_csv("C_test_IT001E00000001.csv", CSV_ESEMPIO)
    risultati = parse_curve_zip(zip_bytes)

    assert "IT001E00000001" in risultati
    punti = risultati["IT001E00000001"].punti
    assert len(punti) == 3
    assert punti[0].valore_kwh == pytest.approx(0.025)
    assert punti[0].timestamp.hour == 0
    assert punti[0].timestamp.minute == 0
    assert punti[1].timestamp.minute == 15


def test_parse_curve_zip_decimali_con_virgola():
    zip_bytes = _zip_da_csv("C_test.csv", CSV_ESEMPIO)
    risultati = parse_curve_zip(zip_bytes)
    punti = risultati["IT001E00000001"].punti
    # "0,037" deve diventare 0.037, non essere scartato o interpretato male
    assert any(p.valore_kwh == pytest.approx(0.037) for p in punti)


def test_parse_curve_zip_righe_vuote_saltate():
    csv_con_vuota = CSV_ESEMPIO + "IT001E00000001;20260501;004500;2;;;;;;0,0;5,004;;E;\n"
    zip_bytes = _zip_da_csv("C_test.csv", csv_con_vuota)
    risultati = parse_curve_zip(zip_bytes)
    # La riga con ATTIVA_PRELEVATA vuoto va saltata, non deve alzare errori
    # né generare un punto con valore None/vuoto
    assert len(risultati["IT001E00000001"].punti) == 3


def test_parse_curve_zip_timestamp_non_valido_non_blocca_il_resto():
    csv_corrotto = CSV_ESEMPIO + "IT001E00000001;ABCDEFGH;000000;2;0,05;;;;;0,0;5,004;;E;\n"
    zip_bytes = _zip_da_csv("C_test.csv", csv_corrotto)
    risultati = parse_curve_zip(zip_bytes)
    # Le 3 righe valide originali devono comunque essere presenti
    assert len(risultati["IT001E00000001"].punti) == 3


def test_parse_curve_zip_zip_vuoto():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    risultati = parse_curve_zip(buf.getvalue())
    assert risultati == {}


def test_parse_curve_zip_ignora_file_non_csv():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("leggimi.txt.bak", "qualcosa")
        zf.writestr("C_test.csv", CSV_ESEMPIO)
    risultati = parse_curve_zip(buf.getvalue())
    assert "IT001E00000001" in risultati


def test_parse_letture_zip_formato_confermato():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IT001E00000001"
    ws.append(
        [
            "Data lettura",
            "Tipo lettura",
            "Lettura",
            "Tipologia Misura",
            "Matricola Contatore",
            "Tipo Misuratore",
            "Energia",
            "Fascia",
        ]
    )
    ws.append(["30/06/2026", "SALDO", "3309", "Energia attiva F3", "123456", "EE GIS", "Energia attiva", "F3"])

    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("L_test_IT001E00000001.xlsx", xlsx_buf.getvalue())

    risultati = parse_letture_zip(buf.getvalue())
    assert "IT001E00000001" in risultati
    riga = risultati["IT001E00000001"][0]
    assert riga.valore == pytest.approx(3309.0)
    assert riga.fascia == "F3"
    assert riga.data_lettura.month == 6
    assert riga.data_lettura.day == 30


def test_duereti_auth_error_e_sottoclasse_di_duereti_api_error():
    assert issubclass(DuretiAuthError, DuretiApiError)


def test_duereti_api_error_porta_status_e_data():
    err = DuretiApiError("boom", http_status=401, data={"message": "boom"})
    assert err.http_status == 401
    assert err.data == {"message": "boom"}
