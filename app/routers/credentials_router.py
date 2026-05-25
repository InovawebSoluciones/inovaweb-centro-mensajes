"""
routers/credentials_router.py
=============================
Registro y rotacion de credenciales de proveedor por tenant+canal.
Scope: admin:credentials.

Las credenciales se cifran con AES-256-GCM antes de tocar disco.
NUNCA se devuelven en respuestas — solo metadatos (tenant, canal, proveedor,
estado, fechas).
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_auth import AuthContext, require_scope
from app.core.crypto import encrypt_value
from app.core.database import get_db
from app.providers.factory import supported_slugs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/v1", tags=["admin:credentials"])


class CredentialsIn(BaseModel):
    provider_slug: str = Field(..., max_length=50)
    # dict con las claves que el provider espera (ej Resend: api_key, webhook_secret, default_from).
    value: dict[str, Any] = Field(..., min_length=1)
    is_default: bool = True


@router.post("/tenants/{tenant_id}/channels/{channel}/credentials",
             status_code=status.HTTP_201_CREATED)
async def register_credentials(
    tenant_id: UUID,
    channel: Literal["email", "whatsapp", "sms"],
    body: CredentialsIn,
    ctx: AuthContext = Depends(require_scope("admin:credentials")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Multi-tenant guard: comparacion como UUID (audit fix — antes era string
    # compare frágil ante diferencias de casing).
    tenant_id_str = str(tenant_id)
    if tenant_id_str != str(UUID(ctx.tenant_id)) and "*" not in ctx.scopes:
        raise HTTPException(403, "no autorizado para gestionar credenciales de otro tenant")

    if body.provider_slug not in supported_slugs():
        raise HTTPException(422, f"provider_slug no soportado: {body.provider_slug}")

    # Verificar que el proveedor exista en catalogo y atienda el canal.
    cat = (await db.execute(text("""
        SELECT slug, channel, status FROM message_providers
         WHERE slug = :slug AND channel = :ch
    """), {"slug": body.provider_slug, "ch": channel})).mappings().first()
    if not cat:
        raise HTTPException(422, f"proveedor {body.provider_slug} no atiende canal {channel}")

    encrypted = encrypt_value(body.value)

    # Si is_default=true, primero desactivar otros defaults del mismo tenant+canal.
    if body.is_default:
        await db.execute(text("""
            UPDATE tenant_channel_credentials
               SET is_default = false, updated_at = NOW()
             WHERE tenant_id = CAST(:tid AS uuid)
               AND channel = :ch
               AND is_default = true
        """), {"tid": tenant_id_str, "ch": channel})

    # UPSERT: si ya existe (tenant, channel, provider), actualizamos encrypted_value.
    row = (await db.execute(text("""
        INSERT INTO tenant_channel_credentials (
            tenant_id, channel, provider_slug, encrypted_value, is_default, is_active
        ) VALUES (
            CAST(:tid AS uuid), :ch, :prov, :enc, :def, true
        )
        ON CONFLICT (tenant_id, channel, provider_slug)
        DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value,
                      is_default      = EXCLUDED.is_default,
                      is_active       = true,
                      updated_at      = NOW()
        RETURNING id::text AS id, channel, provider_slug, is_default, is_active, updated_at
    """), {
        "tid": tenant_id_str, "ch": channel, "prov": body.provider_slug,
        "enc": encrypted, "def": body.is_default,
    })).mappings().first()
    await db.commit()
    return dict(row)


@router.get("/tenants/{tenant_id}/channels/{channel}/credentials")
async def list_credentials(
    tenant_id: UUID,
    channel: Literal["email", "whatsapp", "sms"],
    ctx: AuthContext = Depends(require_scope("admin:credentials")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant_id_str = str(tenant_id)
    if tenant_id_str != str(UUID(ctx.tenant_id)) and "*" not in ctx.scopes:
        raise HTTPException(403, "no autorizado para leer credenciales de otro tenant")

    rows = (await db.execute(text("""
        SELECT id::text AS id, provider_slug, is_default, is_active, created_at, updated_at
        FROM tenant_channel_credentials
        WHERE tenant_id = CAST(:tid AS uuid) AND channel = :ch
        ORDER BY created_at DESC
    """), {"tid": tenant_id_str, "ch": channel})).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}
