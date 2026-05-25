"""
providers/meta_whatsapp.py
==========================
STUB de Meta Cloud API para WhatsApp Business.

Conserva la interfaz de MessageProvider. Implementacion pendiente para sprint 2.

Activar consiste en implementar send_whatsapp / parse_event / verify_webhook_signature
contra:
  POST https://graph.facebook.com/v20.0/{phone_number_id}/messages
  Auth: Bearer access_token (graph token de la app WhatsApp Business)
  Webhook firma HMAC SHA-256 en header X-Hub-Signature-256 con app_secret.

Credenciales esperadas:
  {
    "access_token":    "EAA...",
    "phone_number_id": "1234567890",
    "app_secret":      "..."
  }
"""

from __future__ import annotations

from typing import Any

from app.providers.base import (
    MessageProvider,
    WebhookEvent,
)


class MetaWhatsappProvider(MessageProvider):
    slug = "meta_whatsapp"
    channel = "whatsapp"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)

    async def send_whatsapp(self, **kwargs) -> Any:
        raise NotImplementedError(
            "MetaWhatsappProvider.send_whatsapp() pendiente. "
            "Implementar contra POST /v20.0/{phone_number_id}/messages."
        )

    def verify_webhook_signature(self, *, headers: dict[str, str], raw_body: bytes) -> None:
        raise NotImplementedError(
            "MetaWhatsappProvider.verify_webhook_signature() pendiente. "
            "HMAC SHA-256 con app_secret sobre raw_body; header X-Hub-Signature-256."
        )

    def parse_event(self, payload: dict) -> WebhookEvent:
        raise NotImplementedError(
            "MetaWhatsappProvider.parse_event() pendiente. "
            "Eventos vienen en payload.entry[].changes[].value.statuses[]."
        )
