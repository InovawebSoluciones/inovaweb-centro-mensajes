"""
routers/webhooks_router.py
==========================
Recepcion de webhooks de proveedores externos.

  POST /webhooks/{provider_slug}

Flujo (post-auditoria):
  1. Carga el message correspondiente al external_message_id del payload.
     (Lookup minimo en BD, sin descifrar credenciales todavia.)
  2. Itera todas las credenciales activas del proveedor (puede haber multi-tenant),
     descifrando una por una hasta que verify_webhook_signature pase.
  3. Verifica que el tenant del mensaje coincide con el tenant de la credencial
     que valido la firma — defensa cross-tenant que la auditoria detecto faltante.
  4. INSERT en message_events (deduplicado por UNIQUE(external_message_id, event_type)).
  5. Si el evento es 'delivered'/'bounced'/'failed', actualiza status del mensaje.

El primer cambio importante respecto a la version anterior: la firma se valida
ANTES de hacer trabajo costoso, y el matching de credencial es por tenant
correcto, no por "primera activa".
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

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


_EVENT_TO_STATUS = {
    "delivered": "delivered",
    "bounced":   "bounced",
    "failed":    "failed",
    "dropped":   "failed",
}


def _extract_external_id(provider_slug: str, payload: dict) -> Optional[str]:
    """Extrae el external_message_id del payload sin validar firma todavia.
    Solo se usa para el lookup inicial; si la firma no valida, no se procesa nada."""
    if provider_slug in ("resend", "sendgrid"):
        data = payload.get("data") or {}
        return data.get("email_id") or data.get("id")
    if provider_slug == "meta_whatsapp":
        # Meta: entry[].changes[].value.statuses[].id
        try:
            entry = (payload.get("entry") or [{}])[0]
            change = (entry.get("changes") or [{}])[0]
            statuses = (change.get("value") or {}).get("statuses") or []
            if statuses:
                return statuses[0].get("id")
        except Exception:  # noqa: BLE001
            return None
        return None
    if provider_slug == "twilio":
        # Twilio: form-urlencoded MessageSid
        return payload.get("MessageSid")
    return None


@router.post("/{provider_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_webhook(provider_slug: str, request: Request) -> None:
    if provider_slug not in supported_slugs():
        raise HTTPException(404, f"provider desconocido: {provider_slug}")

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # Tope defensivo: webhooks razonables no exceden 256KB. Mas alla es DoS.
    if len(raw_body) > 256 * 1024:
        logger.warning("webhook %s: body excede 256KB, rechazado", provider_slug)
        raise HTTPException(413, "payload demasiado grande")

    # Parse payload (sin firma validada todavia — solo para extraer external_id).
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "body no es JSON valido")

    external_id = _extract_external_id(provider_slug, payload)
    if not external_id:
        # Sin id no podemos validar nada. Algunos eventos del proveedor son del
        # sistema (no por mensaje); aceptar silenciosamente sin procesar.
        logger.info("webhook %s sin external_message_id, ignorando", provider_slug)
        return

    # Lookup del mensaje target (sin descifrar credenciales aun).
    async with SessionLocal() as db:
        msg = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, status
            FROM messages WHERE external_message_id = :ext LIMIT 1
        """), {"ext": external_id})).mappings().first()

        if not msg:
            # Mensaje desconocido: ignoramos silenciosamente (puede ser de otro env).
            logger.info(
                "webhook %s recibido para external_id %s sin match en BD",
                provider_slug, external_id,
            )
            return

        target_tenant = msg["tenant_id"]

        # Cargar SOLO la credencial del tenant target. Si no existe, probar con
        # otras credenciales activas (caso multi-tenant: un solo secret de
        # proveedor sirve para validar webhooks de ese proveedor para multiples
        # tenants — Resend funciona asi en algunas configuraciones).
        # Audit fix: nunca volvemos a tomar 'el primero activo' sin verificar
        # que el tenant target coincide al final.
        candidates = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, encrypted_value
            FROM tenant_channel_credentials
            WHERE provider_slug = :slug AND is_active = true
            ORDER BY
              CASE WHEN tenant_id = CAST(:target AS uuid) THEN 0 ELSE 1 END,
              is_default DESC,
              created_at DESC
        """), {"slug": provider_slug, "target": target_tenant})).mappings().all()

    if not candidates:
        logger.warning("webhook %s: no hay credenciales registradas", provider_slug)
        raise HTTPException(404, "credenciales no configuradas para este proveedor")

    # Iterar candidatos; el primero (cuyo tenant_id == target) tiene prioridad.
    valid_credential = None
    parse_error: Optional[str] = None
    event = None
    for cand in candidates:
        creds = decrypt_value(cand["encrypted_value"])
        provider = build_provider(provider_slug, creds)
        try:
            try:
                provider.verify_webhook_signature(headers=headers, raw_body=raw_body)
            except ProviderSignatureError:
                continue  # probar siguiente candidato
            except NotImplementedError:
                raise HTTPException(501, f"webhook {provider_slug} aun no implementado")
            # Firma OK. Parsear evento con este provider.
            try:
                event = provider.parse_event(payload)
            except NotImplementedError:
                raise HTTPException(501, f"webhook {provider_slug} parse aun no implementado")
            except ProviderError as exc:
                parse_error = str(exc)
                event = None
            valid_credential = cand
            break
        finally:
            await provider.aclose()

    if valid_credential is None:
        # Ninguna credencial valido la firma — posible spoofing.
        logger.warning(
            "webhook %s: firma invalida para external_id %s (probadas %d credenciales)",
            provider_slug, external_id, len(candidates),
        )
        raise HTTPException(401, "firma invalida")

    # Cross-tenant guard (audit fix): el tenant que valido la firma DEBE ser
    # el dueño del mensaje. Si no, un tenant podria forjar eventos para otro.
    if valid_credential["tenant_id"] != target_tenant:
        logger.warning(
            "webhook %s: tenant mismatch — credencial tenant=%s, mensaje tenant=%s",
            provider_slug, valid_credential["tenant_id"], target_tenant,
        )
        raise HTTPException(401, "firma invalida")

    if parse_error or event is None:
        raise HTTPException(400, f"payload no parseable: {parse_error or 'unknown'}")

    # Persistir evento + ajustar status del mensaje.
    async with SessionLocal() as db:
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
