"""
providers/factory.py
====================
Factory que instancia el MessageProvider apropiado segun slug.

Uso:
    from app.providers.factory import build_provider

    provider = build_provider("resend", credentials_dict)
    result = await provider.send_email(...)
    await provider.aclose()
"""

from __future__ import annotations

from typing import Any

from app.providers.base import MessageProvider, ProviderError
from app.providers.meta_whatsapp import MetaWhatsappProvider
from app.providers.resend import ResendProvider
from app.providers.sendgrid import SendgridProvider
from app.providers.twilio import TwilioProvider


_REGISTRY: dict[str, type[MessageProvider]] = {
    "resend":        ResendProvider,
    "sendgrid":      SendgridProvider,
    "meta_whatsapp": MetaWhatsappProvider,
    "twilio":        TwilioProvider,
}


def build_provider(slug: str, credentials: dict[str, Any]) -> MessageProvider:
    """Instancia el provider concreto para `slug`. Levanta ProviderError si no existe."""
    cls = _REGISTRY.get(slug)
    if cls is None:
        raise ProviderError(f"provider slug no soportado: {slug!r}")
    return cls(credentials)


def supported_slugs() -> list[str]:
    """Lista de slugs registrados (incluye stubs)."""
    return sorted(_REGISTRY.keys())
