"""
providers/sendgrid.py
=====================
STUB de SendGrid para email transaccional.

Conserva la interfaz de MessageProvider pero levanta NotImplementedError
en send_email / parse_event / verify_webhook_signature hasta que se complete.

Activar consiste en implementar los tres metodos siguiendo la docs:
  https://docs.sendgrid.com/api-reference/mail-send/mail-send
  https://docs.sendgrid.com/for-developers/tracking-events/event
"""

from __future__ import annotations

from typing import Any

from app.providers.base import (
    MessageProvider,
    ProviderError,
    WebhookEvent,
)


class SendgridProvider(MessageProvider):
    slug = "sendgrid"
    channel = "email"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)
        # No abrimos cliente HTTP — el stub no envia ni recibe nada.

    async def send_email(self, **kwargs) -> Any:
        raise NotImplementedError(
            "SendgridProvider.send_email() pendiente. "
            "Implementar contra POST https://api.sendgrid.com/v3/mail/send."
        )

    def verify_webhook_signature(self, *, headers: dict[str, str], raw_body: bytes) -> None:
        raise NotImplementedError(
            "SendgridProvider.verify_webhook_signature() pendiente. "
            "SendGrid firma con ED25519 y header X-Twilio-Email-Event-Webhook-Signature."
        )

    def parse_event(self, payload: dict) -> WebhookEvent:
        raise NotImplementedError(
            "SendgridProvider.parse_event() pendiente. "
            "SendGrid emite arrays de eventos; iterar y normalizar uno por uno."
        )
