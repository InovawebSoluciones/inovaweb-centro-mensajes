"""
ledger_client.py
================
Cliente HTTP async hacia el Finanzas-Core (https://finanzas.inovaweb.com.mx).

Responsabilidad:
  Al despachar exitosamente un mensaje, el centro emite un POST al ledger
  consolidado registrando el cargo en la fuente `messages` direccion `debit`.
  Contrato: docs/01-finanzas-core-integracion-cores.md del finanzas-core.

  POST /v1/ledger/entries
    {
      "source_slug": "messages",
      "source_ref":  "msg-<channel>-<message_id>",     <-- idempotente
      "direction":   "debit",
      "amount_cents": <int>,
      "currency":    "MXN",
      "occurred_at": "<ISO 8601 con zona>",
      "description": "<texto auditable corto>",
      "meta":        { "app_id", "client_id", "service_id",
                       "template_id" | "origin_kind",
                       "external_message_id" }
    }

Auth: header `X-API-Key` con FINANZAS_API_KEY (scope ledger:write, etiqueta
`core-messages` en el finanzas-core).

Mapeo de errores:
  - 401/403  -> LedgerAuthError       (key invalida o scope insuficiente)
  - 422      -> LedgerValidationError (bug del centro — NO reintentar)
  - 5xx/red  -> LedgerTransientError  (reintentable por el job)
  - otros    -> LedgerError           (fatal -> ledger_status='manual')

La idempotencia esta garantizada por el finanzas-core en el `source_ref`.
Reintentar el mismo POST devuelve `idempotent_replay=true` sin duplicar.

Fuente de verdad: https://finanzas.inovaweb.com.mx/docs
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from uuid import UUID

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


# ── Excepciones tipadas ──────────────────────────────────────────────────────

class LedgerError(Exception):
    """Error base. Fatal: pasa a ledger_status='manual'."""


class LedgerAuthError(LedgerError):
    """401 o 403 — API key revocada o sin scope ledger:write. No reintentar."""


class LedgerValidationError(LedgerError):
    """422 — body invalido. Bug del centro. NO reintentar."""


class LedgerTransientError(LedgerError):
    """5xx o error de red — reintentable por el job."""


# ── Cliente ───────────────────────────────────────────────────────────────────

class LedgerClient:
    """
    Cliente delgado sobre httpx.AsyncClient. Singleton de proceso.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = (base_url or settings.finanzas_base_url).rstrip("/")
        self._api_key = api_key or settings.finanzas_api_key
        if not self._api_key:
            logger.warning(
                "FINANZAS_API_KEY vacia: las llamadas al ledger fallaran con 401."
            )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            headers={
                "X-API-Key": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "inovaweb-centro-mensajes/0.1",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def record_entry(
        self,
        *,
        source_ref: str,
        amount_cents: int,
        currency: str,
        occurred_at_iso: str,
        description: str,
        meta: Optional[dict[str, Any]] = None,
        direction: str = "debit",
    ) -> dict[str, Any]:
        """
        POST /v1/ledger/entries — registra una entry idempotente.

        Retorna el dict completo de respuesta del finanzas-core, que incluye:
          - id                  UUID de la entry
          - idempotent_replay   bool (true si era un reintento del mismo source_ref)
          - recorded_at         timestamp server-side
        """
        if amount_cents <= 0:
            raise LedgerError(f"amount_cents debe ser > 0, recibido {amount_cents}")
        if direction not in ("credit", "debit"):
            raise LedgerError(f"direction invalido: {direction}")

        body: dict[str, Any] = {
            "source_slug": "messages",
            "source_ref":  source_ref[:120],
            "direction":   direction,
            "amount_cents": int(amount_cents),
            "currency":    currency.upper()[:3],
            "occurred_at": occurred_at_iso,
            "description": (description or "Mensaje despachado")[:500],
            "meta":        meta or {},
        }

        try:
            resp = await self._client.post("/v1/ledger/entries", json=body)
        except httpx.HTTPError as exc:
            raise LedgerTransientError(f"red caida al POST /v1/ledger/entries: {exc}") from exc

        self._raise_for_status(resp, context="record_entry")
        data = resp.json()
        if "id" not in data:
            raise LedgerError(f"respuesta del ledger sin id: {data!r}")
        # Sanity check: aseguremos que entendemos el id como UUID.
        try:
            UUID(data["id"])
        except Exception as exc:
            raise LedgerError(f"id del ledger no es UUID valido: {data['id']!r}") from exc
        return data

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
        """Traduce HTTP status a excepciones tipadas del dominio."""
        if 200 <= resp.status_code < 300:
            return

        body_preview = _redact(resp.text)[:500] if resp.text else "<vacio>"
        msg = f"ledger.{context} HTTP {resp.status_code}: {body_preview}"

        if resp.status_code in (401, 403):
            raise LedgerAuthError(msg)
        if resp.status_code == 422:
            raise LedgerValidationError(msg)
        if resp.status_code >= 500:
            raise LedgerTransientError(msg)
        raise LedgerError(msg)


# ── Helpers ───────────────────────────────────────────────────────────────────

_REDACT_PATTERNS = [
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(X-API-Key\"?\s*[:=]\s*\"?)[^\"\s,}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[^\"\s,}]+"), r"\1<redacted>"),
    (re.compile(r"(fz_[a-z]+_)[A-Za-z0-9]+"), r"\1<redacted>"),
]


def _redact(text: str) -> str:
    """Elimina tokens y API keys de cualquier blob antes de log/persist."""
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


# ── Singleton de proceso ──────────────────────────────────────────────────────

_client_singleton: Optional[LedgerClient] = None


def get_ledger_client() -> LedgerClient:
    """Acceso al singleton. Crea uno la primera vez."""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = LedgerClient()
    return _client_singleton


async def shutdown_ledger_client() -> None:
    """Cierra el pool HTTP. Llamar en el shutdown event de FastAPI."""
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None


def source_ref_for(channel: str, message_id: str) -> str:
    """Construye el source_ref idempotente: msg-<channel>-<message_id>."""
    return f"msg-{channel}-{message_id}"
