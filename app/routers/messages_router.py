"""
routers/messages_router.py
==========================
Endpoints de despacho y consulta de mensajes.

  POST /v1/messages/email      despacha correo (scope messages:write)
  POST /v1/messages/whatsapp   despacha whatsapp
  POST /v1/messages/sms        despacha sms
  GET  /v1/messages            listado paginado multi-eje
  GET  /v1/messages/{id}       detalle con eventos
  GET  /v1/reports/usage       agregados por canal/cliente/periodo

Reglas firmes (contrato en docs/01-centro-mensajes-integracion-cores.md):
  - tenant_id NUNCA viene en body — se resuelve de la X-API-Key.
  - origin_kind obligatorio en email (template | ai_generated).
  - Telefonos en E.164.
  - message_id es UUID server-side.
  - Respuesta inmediata 202 (queued); el cargo al ledger ocurre en el flujo
    de despacho. Si el ledger esta caido, queda en ledger_status='failed'
    y el job ledger_retry lo reintenta.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_auth import AuthContext, require_scope
from app.core.crypto import decrypt_value
from app.core.database import get_db
from app.core.ledger_client import (
    LedgerAuthError,
    LedgerError,
    LedgerTransientError,
    LedgerValidationError,
    get_ledger_client,
    source_ref_for,
)
from app.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderTransientError,
    ProviderValidationError,
)
from app.providers.factory import build_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])


# ── Constantes de catalogo (placeholder hasta admin-financiera) ──────────────
# Catalogo plano por canal en centavos. admin-financiera Nivel 2 lo sustituira.
DEFAULT_PRICES_CENTS: dict[str, int] = {
    "email":    50,
    "whatsapp": 100,
    "sms":      150,
}

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


# ── Schemas pydantic ──────────────────────────────────────────────────────────

class FromEmail(BaseModel):
    email: EmailStr
    name: Optional[str] = Field(None, max_length=200)


class ToEmail(BaseModel):
    email: EmailStr
    name: Optional[str] = Field(None, max_length=200)


class TrackingFlags(BaseModel):
    open: bool = False
    click: bool = False


class EmailIn(BaseModel):
    app_id: Optional[str] = Field(None, max_length=80)
    client_id: Optional[str] = Field(None, max_length=120)
    service_id: Optional[str] = Field(None, max_length=120)
    origin_kind: Literal["template", "ai_generated"]
    # template kind
    template_id: Optional[str] = Field(None, max_length=120)   # slug, no UUID
    variables: Optional[dict[str, Any]] = None
    # ai_generated kind
    subject: Optional[str] = Field(None, max_length=500)
    body_html: Optional[str] = Field(None, max_length=200_000)
    body_text: Optional[str] = Field(None, max_length=200_000)
    # comunes
    from_: FromEmail = Field(..., alias="from")
    to: ToEmail
    tracking: Optional[TrackingFlags] = None
    meta: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_origin_shape(self):
        if self.origin_kind == "template":
            if not self.template_id:
                raise ValueError("origin_kind=template requiere template_id")
        else:  # ai_generated
            if not self.subject:
                raise ValueError("origin_kind=ai_generated requiere subject")
            if not (self.body_html or self.body_text):
                raise ValueError("origin_kind=ai_generated requiere body_html o body_text")
        return self


class WhatsappIn(BaseModel):
    app_id: Optional[str] = Field(None, max_length=80)
    client_id: Optional[str] = Field(None, max_length=120)
    service_id: Optional[str] = Field(None, max_length=120)
    template_id: Optional[str] = Field(None, max_length=120)
    variables: Optional[dict[str, Any]] = None
    from_phone_id: str = Field(..., max_length=30)
    to_phone: str = Field(..., max_length=30)
    message_text: Optional[str] = Field(None, max_length=4096)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_phones(self):
        for label, value in (("from_phone_id", self.from_phone_id), ("to_phone", self.to_phone)):
            if not E164_RE.match(value):
                raise ValueError(f"{label} debe estar en formato E.164 (+<pais><numero>)")
        if not self.template_id and not self.message_text:
            raise ValueError("WhatsApp requiere template_id o message_text")
        return self


class SmsIn(BaseModel):
    app_id: Optional[str] = Field(None, max_length=80)
    client_id: Optional[str] = Field(None, max_length=120)
    service_id: Optional[str] = Field(None, max_length=120)
    from_phone_id: str = Field(..., max_length=30)
    to_phone: str = Field(..., max_length=30)
    message: str = Field(..., min_length=1, max_length=1600)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_phones(self):
        for label, value in (("from_phone_id", self.from_phone_id), ("to_phone", self.to_phone)):
            if not E164_RE.match(value):
                raise ValueError(f"{label} debe estar en formato E.164 (+<pais><numero>)")
        return self


class MessageQueuedOut(BaseModel):
    message_id: str
    tenant_id: str
    channel: str
    status: str
    queued_at: datetime


# ── Helpers internos ──────────────────────────────────────────────────────────

async def _resolve_credentials(
    db: AsyncSession, tenant_id: str, channel: str
) -> tuple[str, dict[str, Any]]:
    """Lee credenciales default del tenant+canal y las descifra. Retorna (provider_slug, creds)."""
    row = (await db.execute(text("""
        SELECT provider_slug, encrypted_value
        FROM tenant_channel_credentials
        WHERE tenant_id = CAST(:tid AS uuid)
          AND channel   = :ch
          AND is_default = true
          AND is_active = true
        LIMIT 1
    """), {"tid": tenant_id, "ch": channel})).mappings().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No hay credencial default configurada para tenant+canal={channel}",
        )
    creds = decrypt_value(row["encrypted_value"])
    return row["provider_slug"], creds


async def _load_template(
    db: AsyncSession,
    tenant_id: str,
    template_slug: str,
    channel: str,
) -> dict[str, Any]:
    """Carga la version activa mas alta de la plantilla. 404 si no existe."""
    row = (await db.execute(text("""
        SELECT id::text AS id, slug, version, channel,
               subject_template, body_html_template, body_text_template, message_template,
               variables_schema
        FROM templates
        WHERE tenant_id = CAST(:tid AS uuid)
          AND slug = :slug
          AND channel = :ch
          AND is_active = true
        ORDER BY version DESC
        LIMIT 1
    """), {"tid": tenant_id, "slug": template_slug, "ch": channel})).mappings().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template_id no existe o esta inactiva: {template_slug}",
        )
    return dict(row)


def _render_template(tpl_str: Optional[str], variables: dict[str, Any]) -> Optional[str]:
    """Renderizado minimo via str.format_map (substitucion de {var})."""
    if tpl_str is None:
        return None
    try:
        return tpl_str.format_map(_SafeDict(variables or {}))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Error renderizando plantilla: {exc}",
        )


class _SafeDict(dict):
    """dict que devuelve '{key}' literal si la clave falta, en vez de KeyError."""
    def __missing__(self, key):
        return "{" + key + "}"


def _short_destination(channel: str, to_email: Optional[str], to_phone: Optional[str]) -> str:
    """Para `description` del ledger: 'foo@b...' o '+52...7890'."""
    if channel == "email" and to_email:
        if "@" in to_email:
            local, _, domain = to_email.partition("@")
            return f"{local[:2]}***@{domain}"
        return to_email[:5] + "***"
    if to_phone:
        return to_phone[:5] + "***" + to_phone[-2:]
    return "<destino>"


async def _record_ledger_entry(
    db: AsyncSession,
    *,
    message_id: str,
    channel: str,
    amount_cents: int,
    currency: str,
    description: str,
    meta: dict[str, Any],
    occurred_at: datetime,
) -> None:
    """
    Intenta el POST al finanzas-core. Persiste el resultado en columnas ledger_*.
    NO levanta excepcion ante fallo transitorio — deja la entry pendiente para
    que el worker ledger_retry la reintente.
    """
    request_id = source_ref_for(channel, message_id)
    client = get_ledger_client()
    occurred_iso = occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    new_status = "pending"
    entry_id: Optional[str] = None
    last_error: Optional[str] = None
    try:
        response = await client.record_entry(
            source_ref=request_id,
            amount_cents=amount_cents,
            currency=currency,
            occurred_at_iso=occurred_iso,
            description=description,
            meta=meta,
        )
        entry_id = response.get("id")
        new_status = "recorded"
    except LedgerValidationError as exc:
        new_status = "manual"
        last_error = f"validation: {exc}"
        logger.error("ledger validation error for message %s: %s", message_id, exc)
    except LedgerAuthError as exc:
        new_status = "manual"
        last_error = f"auth: {exc}"
        logger.error("ledger auth error for message %s: %s", message_id, exc)
    except LedgerTransientError as exc:
        new_status = "failed"
        last_error = f"transient: {exc}"
        logger.warning("ledger transient error for message %s: %s", message_id, exc)
    except LedgerError as exc:
        new_status = "manual"
        last_error = f"error: {exc}"
        logger.error("ledger error for message %s: %s", message_id, exc)

    await db.execute(text("""
        UPDATE messages
           SET ledger_request_id = :rid,
               ledger_entry_id   = CAST(:eid AS uuid),
               ledger_status     = :st,
               ledger_attempts   = ledger_attempts + 1,
               ledger_last_attempt_at = NOW(),
               ledger_last_error = :err,
               ledger_recorded_at = CASE WHEN :st = 'recorded' THEN NOW() ELSE NULL END
         WHERE id = CAST(:mid AS uuid)
    """), {
        "rid": request_id,
        "eid": entry_id,
        "st": new_status,
        "err": last_error,
        "mid": message_id,
    })
    await db.commit()


# ── POST /v1/messages/email ──────────────────────────────────────────────────

@router.post(
    "/v1/messages/email",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageQueuedOut,
)
async def send_email(
    body: EmailIn,
    ctx: AuthContext = Depends(require_scope("messages:write")),
    db: AsyncSession = Depends(get_db),
) -> MessageQueuedOut:
    tenant_id = ctx.tenant_id
    message_id = str(uuid4())

    # Resolver template si aplica.
    tpl_id: Optional[str] = None
    tpl_slug: Optional[str] = None
    tpl_version: Optional[int] = None
    subject = body.subject
    body_html = body.body_html
    body_text = body.body_text

    if body.origin_kind == "template":
        tpl = await _load_template(db, tenant_id, body.template_id, "email")
        tpl_id = tpl["id"]
        tpl_slug = tpl["slug"]
        tpl_version = tpl["version"]
        variables = body.variables or {}
        subject = _render_template(tpl["subject_template"], variables) or subject
        body_html = _render_template(tpl["body_html_template"], variables) or body_html
        body_text = _render_template(tpl["body_text_template"], variables) or body_text

    if not subject:
        raise HTTPException(422, "subject vacio tras renderizar la plantilla")
    if not body_html and not body_text:
        raise HTTPException(422, "body_html o body_text requerido tras renderizar")

    # INSERT en estado queued.
    queued_at = datetime.now(timezone.utc)
    await db.execute(text("""
        INSERT INTO messages (
            id, tenant_id, app_id, client_id, service_id,
            channel, origin_kind,
            template_id, template_slug, template_version,
            from_email, from_name,
            to_email, to_name,
            subject, body_html, body_text,
            variables, tracking_open, tracking_click,
            meta, status, queued_at,
            actor_api_key_id
        ) VALUES (
            CAST(:id AS uuid), CAST(:tid AS uuid), :app, :cli, :svc,
            'email', :okind,
            CAST(:tpl_id AS uuid), :tpl_slug, :tpl_ver,
            :from_email, :from_name,
            :to_email, :to_name,
            :subject, :body_html, :body_text,
            CAST(:vars AS jsonb), :track_open, :track_click,
            CAST(:meta AS jsonb), 'queued', :qat,
            CAST(:akid AS uuid)
        )
    """), {
        "id": message_id, "tid": tenant_id,
        "app": body.app_id, "cli": body.client_id, "svc": body.service_id,
        "okind": body.origin_kind,
        "tpl_id": tpl_id, "tpl_slug": tpl_slug, "tpl_ver": tpl_version,
        "from_email": body.from_.email, "from_name": body.from_.name,
        "to_email": body.to.email, "to_name": body.to.name,
        "subject": subject, "body_html": body_html, "body_text": body_text,
        "vars": _json(body.variables),
        "track_open":  bool(body.tracking and body.tracking.open),
        "track_click": bool(body.tracking and body.tracking.click),
        "meta": _json(body.meta or {}),
        "qat": queued_at,
        "akid": ctx.api_key_id,
    })
    await db.commit()

    # Despachar via proveedor.
    await _dispatch_email(
        db,
        tenant_id=tenant_id,
        message_id=message_id,
        from_email=body.from_.email,
        from_name=body.from_.name,
        to_email=body.to.email,
        to_name=body.to.name,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        origin_kind=body.origin_kind,
        tpl_slug=tpl_slug,
        meta_caller=body.meta or {},
        queued_at=queued_at,
    )

    return MessageQueuedOut(
        message_id=message_id,
        tenant_id=tenant_id,
        channel="email",
        status="queued",
        queued_at=queued_at,
    )


async def _dispatch_email(
    db: AsyncSession,
    *,
    tenant_id: str,
    message_id: str,
    from_email: str,
    from_name: Optional[str],
    to_email: str,
    to_name: Optional[str],
    subject: str,
    body_html: Optional[str],
    body_text: Optional[str],
    origin_kind: str,
    tpl_slug: Optional[str],
    meta_caller: dict[str, Any],
    queued_at: datetime,
) -> None:
    """Resuelve provider, envia, actualiza status y reporta al ledger."""
    provider_slug, creds = await _resolve_credentials(db, tenant_id, "email")
    provider = build_provider(provider_slug, creds)
    try:
        try:
            result = await provider.send_email(
                from_email=from_email,
                from_name=from_name,
                to_email=to_email,
                to_name=to_name,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
            )
        except (ProviderValidationError, ProviderAuthError) as exc:
            await db.execute(text("""
                UPDATE messages SET status='failed', failed_at=NOW(),
                       last_error=:err, provider_slug=:ps
                 WHERE id = CAST(:mid AS uuid)
            """), {"err": str(exc)[:1000], "ps": provider_slug, "mid": message_id})
            await db.commit()
            return
        except ProviderTransientError as exc:
            # Mantener queued; el worker (futuro) reintentaria. Por ahora marcamos failed
            # con last_error para diagnostico inmediato.
            await db.execute(text("""
                UPDATE messages SET status='failed', failed_at=NOW(),
                       last_error=:err, provider_slug=:ps
                 WHERE id = CAST(:mid AS uuid)
            """), {"err": f"transient: {exc}"[:1000], "ps": provider_slug, "mid": message_id})
            await db.commit()
            return
        except ProviderError as exc:
            await db.execute(text("""
                UPDATE messages SET status='failed', failed_at=NOW(),
                       last_error=:err, provider_slug=:ps
                 WHERE id = CAST(:mid AS uuid)
            """), {"err": str(exc)[:1000], "ps": provider_slug, "mid": message_id})
            await db.commit()
            return
    finally:
        await provider.aclose()

    # Marcar sent + setear amount_cents_charged + external_message_id.
    amount = DEFAULT_PRICES_CENTS["email"]
    await db.execute(text("""
        UPDATE messages
           SET status='sent', sent_at=NOW(),
               provider_slug=:ps,
               external_message_id=:ext,
               amount_cents_charged=:amt,
               ledger_status='pending'
         WHERE id = CAST(:mid AS uuid)
    """), {
        "ps": provider_slug,
        "ext": result.external_message_id,
        "amt": amount,
        "mid": message_id,
    })
    await db.commit()

    # POST al finanzas-core (intento inmediato; el worker retoma fallos).
    description = (
        f"Email enviado a {_short_destination('email', to_email, None)} "
        f"via {tpl_slug or 'ai_generated'}"
    )
    meta = {
        "app_id":     meta_caller.get("app_id"),
        "client_id":  meta_caller.get("client_id"),
        "service_id": meta_caller.get("service_id"),
        "template_id": tpl_slug,
        "origin_kind": origin_kind,
        "external_message_id": result.external_message_id,
        # Pasamos tambien los meta del caller (medidor_event_id, model, tokens).
        **{k: v for k, v in meta_caller.items() if k not in ("app_id", "client_id", "service_id")},
    }
    await _record_ledger_entry(
        db,
        message_id=message_id,
        channel="email",
        amount_cents=amount,
        currency="MXN",
        description=description,
        meta=meta,
        occurred_at=queued_at,
    )


# ── POST /v1/messages/whatsapp ───────────────────────────────────────────────

@router.post(
    "/v1/messages/whatsapp",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageQueuedOut,
)
async def send_whatsapp(
    body: WhatsappIn,
    ctx: AuthContext = Depends(require_scope("messages:write")),
    db: AsyncSession = Depends(get_db),
) -> MessageQueuedOut:
    """Envio WhatsApp. En sprint 1, sin proveedor activo: solo encola en BD."""
    tenant_id = ctx.tenant_id
    message_id = str(uuid4())
    queued_at = datetime.now(timezone.utc)

    await db.execute(text("""
        INSERT INTO messages (
            id, tenant_id, app_id, client_id, service_id, channel,
            template_slug, from_phone_id, to_phone, message_text,
            variables, meta, status, queued_at, actor_api_key_id
        ) VALUES (
            CAST(:id AS uuid), CAST(:tid AS uuid),
            :app, :cli, :svc, 'whatsapp',
            :tpl, :from, :to, :msg,
            CAST(:vars AS jsonb), CAST(:meta AS jsonb),
            'queued', :qat, CAST(:akid AS uuid)
        )
    """), {
        "id": message_id, "tid": tenant_id,
        "app": body.app_id, "cli": body.client_id, "svc": body.service_id,
        "tpl": body.template_id,
        "from": body.from_phone_id, "to": body.to_phone, "msg": body.message_text,
        "vars": _json(body.variables),
        "meta": _json(body.meta or {}),
        "qat": queued_at,
        "akid": ctx.api_key_id,
    })
    await db.commit()
    # Proveedor stub: dejamos queued con last_error informativo.
    await db.execute(text("""
        UPDATE messages SET status='failed', failed_at=NOW(),
               last_error='whatsapp provider not implemented yet (sprint 2)'
         WHERE id = CAST(:mid AS uuid)
    """), {"mid": message_id})
    await db.commit()

    return MessageQueuedOut(
        message_id=message_id, tenant_id=tenant_id,
        channel="whatsapp", status="queued", queued_at=queued_at,
    )


# ── POST /v1/messages/sms ────────────────────────────────────────────────────

@router.post(
    "/v1/messages/sms",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageQueuedOut,
)
async def send_sms(
    body: SmsIn,
    ctx: AuthContext = Depends(require_scope("messages:write")),
    db: AsyncSession = Depends(get_db),
) -> MessageQueuedOut:
    """Envio SMS. En sprint 1, sin proveedor activo: solo encola en BD."""
    tenant_id = ctx.tenant_id
    message_id = str(uuid4())
    queued_at = datetime.now(timezone.utc)

    await db.execute(text("""
        INSERT INTO messages (
            id, tenant_id, app_id, client_id, service_id, channel,
            from_phone_id, to_phone, message_text,
            meta, status, queued_at, actor_api_key_id
        ) VALUES (
            CAST(:id AS uuid), CAST(:tid AS uuid),
            :app, :cli, :svc, 'sms',
            :from, :to, :msg,
            CAST(:meta AS jsonb), 'queued', :qat, CAST(:akid AS uuid)
        )
    """), {
        "id": message_id, "tid": tenant_id,
        "app": body.app_id, "cli": body.client_id, "svc": body.service_id,
        "from": body.from_phone_id, "to": body.to_phone, "msg": body.message,
        "meta": _json(body.meta or {}),
        "qat": queued_at,
        "akid": ctx.api_key_id,
    })
    await db.commit()
    await db.execute(text("""
        UPDATE messages SET status='failed', failed_at=NOW(),
               last_error='sms provider not implemented yet (sprint 2)'
         WHERE id = CAST(:mid AS uuid)
    """), {"mid": message_id})
    await db.commit()

    return MessageQueuedOut(
        message_id=message_id, tenant_id=tenant_id,
        channel="sms", status="queued", queued_at=queued_at,
    )


# ── GET /v1/messages ─────────────────────────────────────────────────────────

@router.get("/v1/messages")
async def list_messages(
    ctx: AuthContext = Depends(require_scope("messages:read")),
    db: AsyncSession = Depends(get_db),
    app: Optional[str] = Query(None),
    client: Optional[str] = Query(None),
    service: Optional[str] = Query(None),
    channel: Optional[Literal["email", "whatsapp", "sms"]] = Query(None),
    status_eq: Optional[str] = Query(None, alias="status"),
    from_ts: Optional[datetime] = Query(None),
    to_ts: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, Any] = {"tid": ctx.tenant_id, "limit": limit, "offset": offset}
    if app:        where.append("app_id = :app");        params["app"] = app
    if client:     where.append("client_id = :cli");     params["cli"] = client
    if service:    where.append("service_id = :svc");    params["svc"] = service
    if channel:    where.append("channel = :ch");        params["ch"] = channel
    if status_eq:  where.append("status = :st");         params["st"] = status_eq
    if from_ts:    where.append("created_at >= :fts");   params["fts"] = from_ts
    if to_ts:      where.append("created_at <  :tts");   params["tts"] = to_ts

    where_sql = " AND ".join(where)

    rows = (await db.execute(text(f"""
        SELECT id::text AS id, channel, origin_kind, status,
               app_id, client_id, service_id,
               to_email, to_phone, subject,
               external_message_id, provider_slug,
               amount_cents_charged, currency,
               ledger_status, queued_at, sent_at, delivered_at, failed_at
        FROM messages
        WHERE {where_sql}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)).mappings().all()

    total = (await db.execute(text(f"""
        SELECT COUNT(*) AS c FROM messages WHERE {where_sql}
    """), params)).scalar() or 0

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── GET /v1/messages/{id} ────────────────────────────────────────────────────

@router.get("/v1/messages/{message_id}")
async def get_message(
    message_id: UUID,
    ctx: AuthContext = Depends(require_scope("messages:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = (await db.execute(text("""
        SELECT id::text AS id, tenant_id::text, channel, origin_kind, status,
               app_id, client_id, service_id,
               template_slug, template_version,
               from_email, from_name, from_phone_id,
               to_email, to_name, to_phone,
               subject, provider_slug, external_message_id,
               amount_cents_charged, currency,
               tracking_open, tracking_click,
               events_count, last_error,
               ledger_request_id, ledger_status, ledger_attempts,
               ledger_last_attempt_at, ledger_last_error,
               queued_at, sent_at, delivered_at, failed_at, created_at
        FROM messages
        WHERE id = CAST(:mid AS uuid)
          AND tenant_id = CAST(:tid AS uuid)
        LIMIT 1
    """), {"mid": str(message_id), "tid": ctx.tenant_id})).mappings().first()

    if not row:
        # No diferenciar "no existe" de "no es tuyo" (IDOR guard).
        raise HTTPException(404, "mensaje no encontrado")

    events = (await db.execute(text("""
        SELECT id::text AS id, event_type, occurred_at, url_clicked, provider_slug
        FROM message_events
        WHERE message_id = CAST(:mid AS uuid)
        ORDER BY occurred_at ASC
    """), {"mid": str(message_id)})).mappings().all()

    out = dict(row)
    out["events"] = [dict(e) for e in events]
    return out


# ── GET /v1/reports/usage ────────────────────────────────────────────────────

@router.get("/v1/reports/usage")
async def reports_usage(
    ctx: AuthContext = Depends(require_scope("messages:read")),
    db: AsyncSession = Depends(get_db),
    from_ts: Optional[datetime] = Query(None),
    to_ts: Optional[datetime] = Query(None),
    group_by: str = Query("channel", pattern=r"^(channel|client|app)(,(channel|client|app))*$"),
) -> dict[str, Any]:
    """Agregados simples para Administracion Financiera y reportes."""
    cols = []
    for g in group_by.split(","):
        cols.append({"channel": "channel", "client": "client_id", "app": "app_id"}[g])
    group_sql = ", ".join(cols)

    where = ["tenant_id = CAST(:tid AS uuid)", "status IN ('sent','delivered','bounced')"]
    params: dict[str, Any] = {"tid": ctx.tenant_id}
    if from_ts: where.append("created_at >= :fts"); params["fts"] = from_ts
    if to_ts:   where.append("created_at <  :tts"); params["tts"] = to_ts
    where_sql = " AND ".join(where)

    rows = (await db.execute(text(f"""
        SELECT {group_sql},
               COUNT(*)::bigint AS count,
               COALESCE(SUM(amount_cents_charged), 0)::bigint AS amount_cents
        FROM messages
        WHERE {where_sql}
        GROUP BY {group_sql}
        ORDER BY count DESC
    """), params)).mappings().all()

    return {
        "from_ts": from_ts,
        "to_ts": to_ts,
        "group_by": group_by.split(","),
        "rows": [dict(r) for r in rows],
    }


# ── Util ─────────────────────────────────────────────────────────────────────

def _json(value: Any) -> str:
    """Serializa a JSON string para CAST a jsonb."""
    import json as _json_mod
    return _json_mod.dumps(value or {}, ensure_ascii=False, default=str)
