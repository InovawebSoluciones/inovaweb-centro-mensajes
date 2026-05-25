"""
providers/base.py
=================
Interface comun para todos los proveedores externos de mensajeria
(Resend, SendGrid, Meta Cloud API, Twilio, etc.).

Toda implementacion concreta hereda de MessageProvider y expone:
  - send_email / send_whatsapp / send_sms  (segun los canales que soporte)
  - verify_webhook_signature              (valida la firma del proveedor)
  - parse_event                           (traduce evento externo al lifecycle interno)

El centro NO conoce los detalles de cada proveedor. La factory en el router
de mensajes resuelve la implementacion via slug almacenado en
tenant_channel_credentials.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


# ── Estructuras de salida normalizadas ──────────────────────────────────────

@dataclass(frozen=True)
class DispatchResult:
    """Resultado de un envio exitoso al proveedor."""
    external_message_id: str       # ID asignado por el proveedor (re_xxx, wamid_xxx, etc.)
    provider_status: str           # Status crudo devuelto por el proveedor (informativo).
    raw_response: dict             # Payload completo (para auditoria si se quiere persistir).


@dataclass(frozen=True)
class WebhookEvent:
    """Evento normalizado al lifecycle interno del centro."""
    # Tipos canonicos: sent | delivered | bounced | dropped | opened | clicked | failed | complaint
    event_type: str
    external_message_id: str
    occurred_at_iso: str           # ISO 8601 con zona
    url_clicked: Optional[str] = None
    raw_payload: Optional[dict] = None


# ── Excepciones tipadas ──────────────────────────────────────────────────────

class ProviderError(Exception):
    """Error base de los providers."""


class ProviderAuthError(ProviderError):
    """Credenciales invalidas del proveedor."""


class ProviderValidationError(ProviderError):
    """El payload viola las reglas del proveedor (destinatario malformado, etc.).
    NO reintentar."""


class ProviderTransientError(ProviderError):
    """Error transitorio (timeout, 5xx, throttling). Reintentable."""


class ProviderSignatureError(ProviderError):
    """La firma del webhook no valida. Posible spoofing — rechazar antes de procesar."""


# ── Interface ────────────────────────────────────────────────────────────────

class MessageProvider(ABC):
    """
    Interface abstracta de un proveedor de mensajeria externo.

    Implementaciones concretas viven en:
      app/providers/resend.py        (email, ACTIVO)
      app/providers/sendgrid.py      (email, stub)
      app/providers/meta_whatsapp.py (whatsapp, stub)
      app/providers/twilio.py        (sms, stub)
    """

    # Slug interno (debe coincidir con message_providers.slug en BD).
    slug: str
    # Canal que atiende este proveedor.
    channel: str

    def __init__(self, credentials: dict[str, Any]) -> None:
        """`credentials` es el dict descifrado desde tenant_channel_credentials."""
        self._credentials = credentials

    # ── Envio ──────────────────────────────────────────────────────────────

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
        raise NotImplementedError(f"{self.slug} no soporta email")

    async def send_whatsapp(
        self,
        *,
        from_phone_id: str,
        to_phone: str,
        template_slug: Optional[str],
        variables: Optional[dict[str, Any]],
        message_text: Optional[str],
    ) -> DispatchResult:
        raise NotImplementedError(f"{self.slug} no soporta whatsapp")

    async def send_sms(
        self,
        *,
        from_phone_id: str,
        to_phone: str,
        message_text: str,
    ) -> DispatchResult:
        raise NotImplementedError(f"{self.slug} no soporta sms")

    # ── Webhooks ───────────────────────────────────────────────────────────

    @abstractmethod
    def verify_webhook_signature(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> None:
        """Valida la firma del webhook. Levanta ProviderSignatureError si falla."""
        ...

    @abstractmethod
    def parse_event(self, payload: dict) -> WebhookEvent:
        """Traduce el payload del proveedor al WebhookEvent normalizado."""
        ...

    # ── Cierre de recursos ─────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Cierra clientes HTTP/sockets. Override si el provider mantiene recursos."""
        return None
