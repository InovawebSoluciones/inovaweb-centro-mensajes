"""
test_tracking_signing.py
========================
Verifica que la firma HMAC de URLs de tracking funciona correctamente:
  - Firma de open y click es deterministica.
  - Firmas distintas para (message_id, url) distintos.
  - verify_* devuelve False para sig faltante, vacia, o invalida.
  - Cambiar la URL invalida la firma de click (anti-tampering).
"""

import pytest

from app.core.tracking_signing import (
    sign_click,
    sign_open,
    verify_click,
    verify_open,
)


def test_open_round_trip():
    mid = "018e2c7b-a1d4-7c2e-9f3a-1234567890ab"
    sig = sign_open(mid)
    assert verify_open(mid, sig) is True


def test_open_sig_diferente_para_message_id_distinto():
    a = sign_open("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    b = sign_open("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    assert a != b


def test_open_verify_rechaza_sig_invalida():
    mid = "018e2c7b-a1d4-7c2e-9f3a-1234567890ab"
    assert verify_open(mid, "wrong") is False
    assert verify_open(mid, "") is False
    assert verify_open(mid, None) is False  # type: ignore[arg-type]


def test_click_round_trip():
    mid = "018e2c7b-a1d4-7c2e-9f3a-1234567890ab"
    url = "https://inovaweb.com.mx/destino"
    sig = sign_click(mid, url)
    assert verify_click(mid, url, sig) is True


def test_click_sig_invalida_si_url_cambia():
    """Anti-tampering: la firma cubre (message_id, url) — no la podes reusar
    con otra URL."""
    mid = "018e2c7b-a1d4-7c2e-9f3a-1234567890ab"
    legit_url = "https://inovaweb.com.mx/legit"
    sig = sign_click(mid, legit_url)
    # Reuso de la firma con URL distinta debe fallar.
    assert verify_click(mid, "https://evil.example/", sig) is False


def test_click_sig_invalida_si_message_id_cambia():
    sig = sign_click("aaa", "https://x.com")
    assert verify_click("bbb", "https://x.com", sig) is False


def test_sig_longitud_22_chars():
    # base64url(16 bytes) sin padding = 22 chars.
    sig = sign_open("018e2c7b-a1d4-7c2e-9f3a-1234567890ab")
    assert len(sig) == 22
