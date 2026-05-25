"""
test_crypto.py
==============
Round-trip de AES-256-GCM y deteccion de tampering.
"""

import pytest

from app.core.crypto import CryptoError, decrypt_value, encrypt_value


def test_round_trip():
    original = {"api_key": "re_test_12345", "default_from": "x@y.com"}
    ciphertext = encrypt_value(original)
    assert ciphertext  # no vacio
    assert "re_test" not in ciphertext  # no leak de plaintext
    decrypted = decrypt_value(ciphertext)
    assert decrypted == original


def test_decrypt_empty_returns_empty_dict():
    assert decrypt_value(None) == {}
    assert decrypt_value("") == {}


def test_decrypt_garbage_raises():
    with pytest.raises(CryptoError):
        decrypt_value("not-base64-at-all-zzz")


def test_decrypt_tampered_raises():
    original = {"k": "v"}
    ciphertext = encrypt_value(original)
    # Cambiar un byte en el blob base64.
    tampered = ciphertext[:-2] + ("A" if ciphertext[-2] != "A" else "B") + ciphertext[-1]
    with pytest.raises(CryptoError):
        decrypt_value(tampered)


def test_encrypt_requires_dict():
    with pytest.raises(CryptoError):
        encrypt_value("string-no-permitido")  # type: ignore[arg-type]


def test_each_encrypt_uses_fresh_nonce():
    # Misma entrada produce ciphertext distinto cada vez (nonce nuevo).
    payload = {"k": "v"}
    a = encrypt_value(payload)
    b = encrypt_value(payload)
    assert a != b
    assert decrypt_value(a) == decrypt_value(b) == payload
