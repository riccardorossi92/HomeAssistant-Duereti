"""Test per la logica delle date in coordinator.py.

A differenza di test_api.py, questi richiedono Home Assistant installato
(coordinator.py importa homeassistant.*), quindi vanno eseguiti in un
ambiente con pytest-homeassistant-custom-component (vedi requirements_test.txt).
"""
from datetime import date

from custom_components.duereti_letture.coordinator import (
    _mese_precedente_completo,
    _range_sei_mesi_precedenti,
)


def test_mese_precedente_completo_caso_normale():
    inizio, fine = _mese_precedente_completo(date(2026, 8, 2))
    assert inizio == date(2026, 7, 1)
    assert fine == date(2026, 7, 31)


def test_mese_precedente_completo_cambio_anno():
    inizio, fine = _mese_precedente_completo(date(2026, 1, 15))
    assert inizio == date(2025, 12, 1)
    assert fine == date(2025, 12, 31)


def test_mese_precedente_completo_febbraio_bisestile():
    # 2028 e' bisestile: marzo 2028 -> mese precedente e' febbraio con 29 giorni
    inizio, fine = _mese_precedente_completo(date(2028, 3, 1))
    assert inizio == date(2028, 2, 1)
    assert fine == date(2028, 2, 29)


def test_range_sei_mesi_precedenti_copre_esattamente_sei_mesi():
    inizio, fine = _range_sei_mesi_precedenti(date(2026, 8, 2))
    n_mesi = (fine.year - inizio.year) * 12 + (fine.month - inizio.month) + 1
    assert n_mesi == 6
    assert inizio == date(2026, 2, 1)
    assert fine == date(2026, 7, 31)


def test_range_sei_mesi_precedenti_cambio_anno():
    inizio, fine = _range_sei_mesi_precedenti(date(2026, 3, 1))
    n_mesi = (fine.year - inizio.year) * 12 + (fine.month - inizio.month) + 1
    assert n_mesi == 6
    assert inizio == date(2025, 9, 1)
    assert fine == date(2026, 2, 28)


def test_range_sei_mesi_precedenti_rispetta_limite_api():
    """Il manuale Duereti impone un range massimo di 6 mesi per requestExport:
    verifichiamo di restare esattamente al limite, mai oltre."""
    from custom_components.duereti_letture.const import MAX_DATE_RANGE_MONTHS

    inizio, fine = _range_sei_mesi_precedenti(date(2026, 8, 2))
    n_mesi_range = (fine.year - inizio.year) * 12 + (fine.month - inizio.month)
    assert n_mesi_range <= MAX_DATE_RANGE_MONTHS
