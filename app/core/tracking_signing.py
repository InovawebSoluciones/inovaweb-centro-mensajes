"""
tracking_signing.py
===================
Firma HMAC-SHA256 para URLs publicas de tracking (pixel de apertura y
redireccion de clicks).

Problema que resuelve:
  Los endpoints `GET /v1/track/email/open/{message_id}` y `/click/{message_id}`
  son publicos (los destinatarios no tienen API key). Sin firma, cualquiera con
  un UUID puede:
    1. Verificar si un message_id existe.
    2. Usar /click?u=<URL> como redirector libre (phishing-as-a-service).

  Con firma, solo URLs emitidas por el centro al renderizar el correo son
  validas. Cualquier intento sin sig o con sig invalida -> 404.

Diseño:
  - Secreto: derivado del AES_KEY del proceso via HKDF-like (HMAC con label).
    No requiere variable de entorno adicional.
  - Algoritmo: HMAC-SHA256 truncado a 16 bytes (128 bits de fuerza), base64url
    sin padding. Longitud de la firma: 22 chars.
  - Para tracking OPEN: firma sobre `f"open|{message_id}"`.
  - Para tracking CLICK: firma sobre `f"click|{message_id}|{url}"`.
    De este modo la firma NO sirve para redirigir a una URL distinta.
  - Comparacion con `hmac.compare_digest` (timing-safe).
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.core.crypto import _key  # reutilizamos AES_KEY como material base


def _signing_key() -> bytes:
    """Deriva una key de firma a partir de AES_KEY + label fijo (HKDF light)."""
    return hmac.new(_key(), b"centro-mensajes:tracking-signing:v1", hashlib.sha256).digest()


def _hmac_b64(message: bytes) -> str:
    """HMAC-SHA256 truncado a 16 bytes -> base64url sin padding."""
    full = hmac.new(_signing_key(), message, hashlib.sha256).digest()
    truncated = full[:16]
    return base64.urlsafe_b64encode(truncated).decode("ascii").rstrip("=")


def sign_open(message_id: str) -> str:
    """Firma para pixel de apertura."""
    return _hmac_b64(f"open|{message_id}".encode("utf-8"))


def sign_click(message_id: str, url: str) -> str:
    """Firma para redireccion de click."""
    return _hmac_b64(f"click|{message_id}|{url}".encode("utf-8"))


def verify_open(message_id: str, sig: str | None) -> bool:
    """True si `sig` es una firma valida del pixel para `message_id`."""
    if not sig:
        return False
    expected = sign_open(message_id)
    return hmac.compare_digest(sig, expected)


def verify_click(message_id: str, url: str, sig: str | None) -> bool:
    """True si `sig` es una firma valida del click para (message_id, url)."""
    if not sig:
        return False
    expected = sign_click(message_id, url)
    return hmac.compare_digest(sig, expected)
