"""
crypto.py
=========
Cifrado AES-256-GCM para credenciales de proveedores externos
(`tenant_channel_credentials.encrypted_value`).

Formato de almacenamiento (string en columna TEXT):
    base64( nonce[12] || ciphertext || tag[16] )

Garantia: GCM ofrece confidencialidad + autenticidad. Si la BD es modificada
sin la `AES_KEY` del proceso, la autenticacion falla y `decrypt_value`
levanta `CryptoError`. No hay forma de leer credenciales con BD comprometida.

`AES_KEY` se lee de settings (.env). Debe ser 32 bytes random codificados
en base64. Generar con:
    python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
"""

from __future__ import annotations

import base64
import json
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


NONCE_BYTES = 12   # GCM recomienda 96 bits


class CryptoError(Exception):
    """Cualquier fallo al cifrar/descifrar/decodear credenciales."""


@lru_cache(maxsize=1)
def _key() -> bytes:
    """
    Decodifica `AES_KEY` de base64 una sola vez por proceso.

    Valida que sea exactamente 32 bytes (AES-256). Fail-fast en arranque.
    """
    try:
        raw = base64.b64decode(settings.aes_key, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"AES_KEY no es base64 valido: {exc}") from exc
    if len(raw) != 32:
        raise CryptoError(
            f"AES_KEY debe ser 32 bytes (256 bits) decodificados. Tiene {len(raw)}."
        )
    return raw


def encrypt_value(plain: dict) -> str:
    """
    Serializa `plain` como JSON UTF-8 y lo cifra con AES-256-GCM.
    Retorna un string base64 listo para INSERT en encrypted_value.
    Cada llamada genera un nonce nuevo — NUNCA reusar nonce con la misma key.
    """
    if not isinstance(plain, dict):
        raise CryptoError("encrypt_value requiere un dict.")
    aes = AESGCM(_key())
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(plain, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext_and_tag = aes.encrypt(nonce, plaintext, associated_data=None)
    blob = nonce + ciphertext_and_tag
    return base64.b64encode(blob).decode("ascii")


def decrypt_value(encrypted: str | None) -> dict:
    """
    Descifra el blob de encrypted_value y devuelve el dict.

    - None o string vacio -> {} (sin credenciales).
    - Bytes invalidos / firma rota -> CryptoError generico.
    """
    if not encrypted:
        return {}

    try:
        blob = base64.b64decode(encrypted, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("encrypted_value no es base64 valido.") from exc

    if len(blob) < NONCE_BYTES + 16:
        raise CryptoError("encrypted_value truncado o corrupto.")

    nonce = blob[:NONCE_BYTES]
    ct_and_tag = blob[NONCE_BYTES:]

    try:
        plaintext = AESGCM(_key()).decrypt(nonce, ct_and_tag, associated_data=None)
    except Exception:
        # Mensaje generico para no revelar si fallo por key o por tampering.
        raise CryptoError("No se pudo descifrar encrypted_value (firma invalida).")

    try:
        data = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"encrypted_value descifrado no es JSON valido: {exc}") from exc

    if not isinstance(data, dict):
        raise CryptoError("encrypted_value descifrado no es un objeto JSON.")
    return data
