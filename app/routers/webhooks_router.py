"""
routers/webhooks_router.py
==========================
Recepcion de webhooks de proveedores externos.

  POST /webhooks/{provider_slug}

Flujo:
  1. Lee raw body + headers (sin parsear todavia).
  2. Resuelve credenciales de cualquier tenant que tenga este proveedor activo
     (los webhooks son globales por proveedor; multi-tenant se resuelve via
     external_message_id que ya esta indexado a un tenant especifico).
  3. Valida la firma con provider.verify_webhook_signature(...).
  4. Parsea evento con provider.parse_event(...).
  5. INSERT en message_events (deduplicado por UNIQUE(external_message_id,
     event_type)).
  6. Si el evento es 'delivered' o 'bounced' o 'failed', actualiza el status
     del mensaje correspondiente.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from app.core.crypto import decrypt_value
from app.core.database import SessionLocal
from app.providers.base import (
    ProviderError,
    ProviderSignatureError,
)
from app.providers.factory import build_provider, supported_slugs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Mapeo de event_type interno al status que debe quedar en messages (si cambia).
_EVENT_TO_STATUS = {
    "delivered": "delivered",
    "bounced":   "bounced",
    "failed":    "failed",
    "dropped":   "failed",
}


@router.post("/{provider_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_webhook(provider_slug: str, request: Request) -> None:
    if provider_slug not in supported_slugs():
        raise HTTPException(404, f"provider desconocido: {provider_slug}")

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # Resolver credenciales: tomamos la PRIMERA credencial activa de este proveedor
    # (los secretos de webhook suelen ser globales del proveedor, no por tenant).
    # Para soporte multi-tenant fino del mismo proveedor, en sprint posterior se
    # iteran todas las credentials activas hasta que una verify_webhook_signature
    # tenga exito.
    async with SessionLocal() as db:
        row = (await db.execute(text("""
            SELECT encrypted_value
            FROM tenant_channel_credentials
            WHERE provider_slug = :slug AND is_active = true
            ORDER BY is_default DESC, created_at DESC
            LIMIT 1
        """), {"slug": provider_slug})).mappings().first()

    if not row:
        logger.warning("webhook %s: no hay credenciales registradas", provider_slug)
        raise HTTPException(404, "credenciales no configuradas para este proveedor")

    creds = decrypt_value(row["encrypted_value"])
    provider = build_provider(provider_slug, creds)

    try:
        try:
            provider.verify_webhook_signature(headers=headers, raw_body=raw_body)
        except ProviderSignatureError as exc:
            logger.warning("webhook %s: firma invalida -- %s", provider_slug, exc)
            raise HTTPException(401, "firma invalida")
        except NotImplementedError:
            # Provider stub: rechazar para no comerse webhooks sin verificar.
            raise HTTPException(501, f"webhook {provider_slug} aun no implementado")

        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            raise HTTPException(400, "body no es JSON valido")

        try:
            event = provider.parse_event(payload)
        except NotImplementedError:
            raise HTTPException(501, f"webhook {provider_slug} parse aun no implementado")
        except ProviderError as exc:
            raise HTTPException(400, f"payload no parseable: {exc}")
    finally:
        await provider.aclose()

    if not event.external_message_id:
        # Algunos eventos del proveedor no corresponden a un mensaje (system events).
        logger.info("webhook %s evento sin external_message_id, ignorando", provider_slug)
        return

    # Persistir el evento (idempotente via UNIQUE) + ajustar status del mensaje.
    async with SessionLocal() as db:
        msg = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, status
            FROM messages WHERE external_message_id = :ext LIMIT 1
        """), {"ext": event.external_message_id})).mappings().first()
        if not msg:
            # Mensaje no encontrado: puede ser de otro entorno o de antes del rollout.
            logger.info(
                "webhook %s recibido para external_message_id %s sin match en BD",
                provider_slug, event.external_message_id,
            )
            return

        try:
            await db.execute(text("""
                INSERT INTO message_events (
                    message_id, tenant_id, event_type, external_message_id,
                    provider_slug, url_clicked, raw_payload, occurred_at
                ) VALUES (
                    CAST(:mid AS uuid), CAST(:tid AS uuid), :et, :ext,
                    :ps, :url, CAST(:raw AS jsonb), COALESCE(:oa::timestamptz, NOW())
                )
                ON CONFLICT (external_message_id, event_type) DO NOTHING
            """), {
                "mid": msg["id"], "tid": msg["tenant_id"], "et": event.event_type,
                "ext": event.external_message_id, "ps": provider_slug,
                "url": event.url_clicked,
                "raw": json.dumps(event.raw_payload or {}, ensure_ascii=False),
                "oa": event.occurred_at_iso or None,
            })

            new_status = _EVENT_TO_STATUS.get(event.event_type)
            if new_status and msg["status"] in ("queued", "sent"):
                ts_col = "delivered_at" if new_status == "delivered" else "failed_at"
                await db.execute(text(f"""
                    UPDATE messages
                       SET status = :st, {ts_col} = NOW()
                     WHERE id = CAST(:mid AS uuid)
                       AND status IN ('queued', 'sent')
                """), {"st": new_status, "mid": msg["id"]})

            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.exception("webhook %s: error persistiendo evento -- %s", provider_slug, exc)
            raise HTTPException(500, "error procesando evento")
