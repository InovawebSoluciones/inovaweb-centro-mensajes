"""
providers/resend.py
===================
Implementacion del proveedor Resend (https://resend.com/docs/api-reference).

Endpoint usado:
  POST https://api.resend.com/emails
  Auth: header `Authorization: Bearer <RESEND_API_KEY>`

Webhooks firmados con svix (https://docs.svix.com/receiving/verifying-payloads/how).
Headers que envia Resend:
  - svix-id
  - svix-timestamp
  - svix-signature   (formato: "v1,<base64-hmac-sha256>" o multiple separadas por espacio)

Tipos de eventos relevantes (https://resend.com/docs/dashboard/webhooks/event-types):
  - email.sent
  - email.delivered
  - email.bounced
  - email.complained
  - email.opened
  - email.clicked
  - email.delivery_delayed

Credenciales esperadas en tenant_channel_credentials.encrypted_value:
  {
    "api_key":        "re_xxxxxxxxxxxxxxxxxxxx",
    "webhook_secret": "whsec_xxxxxxxxxxxxxxxx",   <-- opcional, requerido para webhooks
    "default_from":   "noreply@inovaweb.com.mx"   <-- opcional
  }
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from app.providers.base import (
    DispatchResult,
    MessageProvider,
    ProviderAuthError,
    ProviderError,
    ProviderSignatureError,
    ProviderTransientError,
    ProviderValidationError,
    WebhookEvent,
)

logger = logging.getLogger(__name__)


RESEND_API_BASE = "https://api.resend.com"


# Mapeo de event_type de Resend al lifecycle interno del centro.
_EVENT_MAP = {
    "email.sent":             "sent",
    "email.delivered":        "delivered",
    "email.bounced":          "bounced",
    "email.complained":       "complaint",
    "email.opened":           "opened",
    "email.clicked":          "clicked",
    "email.delivery_delayed": "sent",  # transitorio, mantiene 'sent'
}


class ResendProvider(MessageProvider):
    slug = "resend"
    channel = "email"

    def __init__(self, credentials: dict[str, Any]) -> None:
        super().__init__(credentials)
        api_key = credentials.get("api_key")
        if not api_key:
            raise ProviderAuthError("Resend: falta 'api_key' en credentials.")
        self._api_key = api_key
        self._webhook_secret = credentials.get("webhook_secret")
        self._default_from = credentials.get("default_from")
        self._client = httpx.AsyncClient(
            base_url=RESEND_API_BASE,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "inovaweb-centro-mensajes/0.1",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Envio ────────────────────────────────────────────────────────────────

    async def send_email(
        self,
        *,
        from_email: str,
        from_name: Optional[str],
        to_email: str,
        to_name: Optional[str],
        subject: str,
        body_html: Optional[str],
        body_text: Optional[str],
        headers: Optional[dict[str, str]] = None,
    ) -> DispatchResult:
        # Resend requiere al menos uno de html / text.
        if not body_html and not body_text:
            raise ProviderValidationError("Resend: se requiere body_html o body_text.")

        # Formato Resend: "Nombre <email@dominio>" o solo "email@dominio".
        from_field = (
            f"{from_name} <{from_email}>" if from_name else from_email
        )
        if not from_field:
            from_field = self._default_from
            if not from_field:
                raise ProviderValidationError("Resend: falta from_email y no hay default_from.")

        to_field = (
            f"{to_name} <{to_email}>" if to_name else to_email
        )

        body: dict[str, Any] = {
            "from": from_field,
            "to": [to_field],
            "subject": subject,
        }
        if body_html:
            body["html"] = body_html
        if body_text:
            body["text"] = body_text
        if headers:
            # Resend permite headers personalizados.
            body["headers"] = headers

        try:
            resp = await self._client.post("/emails", json=body)
        except httpx.HTTPError as exc:
            raise ProviderTransientError(f"Resend: red caida -- {exc}") from exc

        if resp.status_code in (401, 403):
            raise ProviderAuthError(f"Resend HTTP {resp.status_code}: credenciales invalidas")
        if resp.status_code == 422 or resp.status_code == 400:
            raise ProviderValidationError(f"Resend HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 500:
            raise ProviderTransientError(f"Resend HTTP {resp.status_code}: {resp.text[:300]}")
        if not (200 <= resp.status_code < 300):
            raise ProviderError(f"Resend HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        external_id = data.get("id")
        if not external_id:
            raise ProviderError(f"Resend: respuesta sin id -- {data!r}")

        return DispatchResult(
            external_message_id=external_id,
            provider_status="accepted",
            raw_response=data,
        )

    # ── Webhooks ───────────────────────────────────────────────────────────

    def verify_webhook_signature(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> None:
        """
        Verifica firma svix de Resend.
        Algoritmo: HMAC-SHA256(secret, f"{svix_id}.{svix_timestamp}.{body}")
        Comparar contra cualquiera de los valores en svix-signature.
        """
        if not self._webhook_secret:
            raise ProviderSignatureError(
                "Resend: webhook_secret no configurado en credentials"
            )

        svix_id = _hget(headers, "svix-id")
        svix_timestamp = _hget(headers, "svix-timestamp")
        svix_signature = _hget(headers, "svix-signature")

        if not svix_id or not svix_timestamp or not svix_signature:
            raise ProviderSignatureError("Resend: faltan headers svix-*")

        # El secret de svix viene con prefijo "whsec_" + base64.
        secret_b64 = self._webhook_secret
        if secret_b64.startswith("whsec_"):
            secret_b64 = secret_b64[len("whsec_"):]
        try:
            secret_bytes = base64.b64decode(secret_b64)
        except Exception as exc:
            raise ProviderSignatureError(f"Resend: webhook_secret no es base64: {exc}") from exc

        signed_payload = f"{svix_id}.{svix_timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
        expected = base64.b64encode(
            hmac.new(secret_bytes, signed_payload, hashlib.sha256).digest()
        ).decode("ascii")

        # svix-signature puede contener multiples firmas: "v1,sig1 v1,sig2"
        for chunk in svix_signature.split(" "):
            parts = chunk.split(",", 1)
            if len(parts) != 2:
                continue
            _version, sig = parts
            if hmac.compare_digest(sig, expected):
                return  # OK

        raise ProviderSignatureError("Resend: firma svix invalida")

    def parse_event(self, payload: dict) -> WebhookEvent:
        """Traduce un payload de webhook de Resend a WebhookEvent."""
        event_type_raw = payload.get("type", "")
        event_type = _EVENT_MAP.get(event_type_raw, "failed")

        data = payload.get("data", {}) or {}
        external_id = data.get("email_id") or data.get("id") or ""
        occurred_at = payload.get("created_at") or data.get("created_at") or ""

        url_clicked = None
        if event_type == "clicked":
            click = data.get("click") or {}
            url_clicked = click.get("link") or click.get("url")

        return WebhookEvent(
            event_type=event_type,
            external_message_id=external_id,
            occurred_at_iso=occurred_at,
            url_clicked=url_clicked,
            raw_payload=payload,
        )


def _hget(headers: dict[str, str], name: str) -> Optional[str]:
    """Lookup case-insensitive en dict de headers."""
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None
