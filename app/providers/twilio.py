"""
providers/twilio.py
===================
STUB de Twilio para SMS (y WhatsApp como alternativa).

Conserva la interfaz de MessageProvider. Implementacion pendiente para sprint 2.

Activar consiste en implementar send_sms / parse_event / verify_webhook_signature
contra:
  POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json
  Auth: HTTP Basic con AccountSid / AuthToken
  Webhook firma HMAC SHA-1 en header X-Twilio-Signature.

Credenciales esperadas:
  {
    "account_sid":         "ACxxxxxxxxxxxxxx",
    "auth_token":          "xxxxxxxxxxxxxxxx",
    "messaging_service_sid": "MGxxxxxxxxxxxxxx"   <-- opcional
  }
"""

from __future__ import annotations

from typing import Any

from app.providers.base import (
    MessageProvider,
    WebhookEvent,
)


class TwilioProvider(MessageProvider):
    slug = "twilio"
    channel = "sms"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)

    async def send_sms(self, **kwargs) -> Any:
        raise NotImplementedError(
            "TwilioProvider.send_sms() pendiente. "
            "Implementar contra POST /2010-04-01/Accounts/{AccountSid}/Messages.json."
        )

    def verify_webhook_signature(self, *, headers: dict[str, str], raw_body: bytes) -> None:
        raise NotImplementedError(
            "TwilioProvider.verify_webhook_signature() pendiente. "
            "HMAC SHA-1 con auth_token sobre URL+sorted-params; header X-Twilio-Signature."
        )

    def parse_event(self, payload: dict) -> WebhookEvent:
        raise NotImplementedError(
            "TwilioProvider.parse_event() pendiente. "
            "Eventos llegan como form-urlencoded con MessageStatus."
        )
