"""
routers/templates_router.py
===========================
CRUD administrativo de plantillas. Scope: admin:templates.

Plantillas son inmutables una vez creadas (trigger en BD).
Correccion = PATCH crea nueva fila con version+=1.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_auth import AuthContext, require_scope
from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/v1", tags=["admin:templates"])


class TemplateIn(BaseModel):
    slug: str = Field(..., min_length=3, max_length=120, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    channel: Literal["email", "whatsapp", "sms"]
    name: str = Field(..., min_length=1, max_length=200)
    subject_template: Optional[str] = Field(None, max_length=500)
    body_html_template: Optional[str] = Field(None, max_length=200_000)
    body_text_template: Optional[str] = Field(None, max_length=200_000)
    message_template: Optional[str] = Field(None, max_length=4096)
    variables_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateIn,
    ctx: AuthContext = Depends(require_scope("admin:templates")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Crea v1 de una plantilla nueva. Si el slug ya existe, devolver conflict."""
    existing = (await db.execute(text("""
        SELECT MAX(version) FROM templates
         WHERE tenant_id = CAST(:tid AS uuid) AND slug = :slug AND channel = :ch
    """), {"tid": ctx.tenant_id, "slug": body.slug, "ch": body.channel})).scalar()
    if existing is not None:
        raise HTTPException(
            409,
            f"Ya existe plantilla {body.slug} (v{existing}). Use PATCH para nueva version.",
        )

    row = (await db.execute(text("""
        INSERT INTO templates (
            tenant_id, slug, version, channel, name,
            subject_template, body_html_template, body_text_template, message_template,
            variables_schema, metadata, is_active, created_by_api_key_id
        ) VALUES (
            CAST(:tid AS uuid), :slug, 1, :ch, :name,
            :subj, :html, :txt, :msg,
            CAST(:vs AS jsonb), CAST(:md AS jsonb), true, CAST(:akid AS uuid)
        )
        RETURNING id::text AS id, slug, version, channel, name, is_active, created_at
    """), {
        "tid": ctx.tenant_id, "slug": body.slug, "ch": body.channel, "name": body.name,
        "subj": body.subject_template, "html": body.body_html_template,
        "txt": body.body_text_template, "msg": body.message_template,
        "vs": json.dumps(body.variables_schema or {}),
        "md": json.dumps(body.metadata or {}),
        "akid": ctx.api_key_id,
    })).mappings().first()
    await db.commit()
    return dict(row)


@router.get("/templates")
async def list_templates(
    ctx: AuthContext = Depends(require_scope("admin:templates")),
    db: AsyncSession = Depends(get_db),
    channel: Optional[Literal["email", "whatsapp", "sms"]] = Query(None),
    slug: Optional[str] = Query(None),
    only_active: bool = Query(True),
) -> dict[str, Any]:
    where = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, Any] = {"tid": ctx.tenant_id}
    if channel: where.append("channel = :ch"); params["ch"] = channel
    if slug:    where.append("slug = :slug");  params["slug"] = slug
    if only_active: where.append("is_active = true")

    rows = (await db.execute(text(f"""
        SELECT id::text AS id, slug, version, channel, name, is_active,
               variables_schema, created_at
        FROM templates
        WHERE {' AND '.join(where)}
        ORDER BY slug ASC, version DESC
    """), params)).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/templates/{template_id}")
async def get_template(
    template_id: UUID,
    ctx: AuthContext = Depends(require_scope("admin:templates")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = (await db.execute(text("""
        SELECT id::text AS id, slug, version, channel, name,
               subject_template, body_html_template, body_text_template, message_template,
               variables_schema, metadata, is_active, created_at
        FROM templates
        WHERE id = CAST(:tid AS uuid) AND tenant_id = CAST(:ten AS uuid)
        LIMIT 1
    """), {"tid": str(template_id), "ten": ctx.tenant_id})).mappings().first()
    if not row:
        raise HTTPException(404, "plantilla no encontrada")
    return dict(row)


class TemplatePatchIn(TemplateIn):
    """Mismo schema. PATCH crea una nueva fila (version += 1)."""


@router.patch("/templates/{template_id}", status_code=status.HTTP_201_CREATED)
async def patch_template(
    template_id: UUID,
    body: TemplatePatchIn,
    ctx: AuthContext = Depends(require_scope("admin:templates")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    PATCH no muta la plantilla existente (trigger en BD lo bloquea).
    En vez de eso, inserta una nueva fila con (slug, version+1) basada en la
    plantilla referenciada. El slug del body se ignora — se toma el existente.
    """
    cur = (await db.execute(text("""
        SELECT slug, channel, version
        FROM templates
        WHERE id = CAST(:tid AS uuid) AND tenant_id = CAST(:ten AS uuid)
        LIMIT 1
    """), {"tid": str(template_id), "ten": ctx.tenant_id})).mappings().first()
    if not cur:
        raise HTTPException(404, "plantilla base no encontrada")

    if body.channel != cur["channel"]:
        raise HTTPException(422, "channel no puede cambiar entre versiones")

    new_version = int(cur["version"]) + 1

    row = (await db.execute(text("""
        INSERT INTO templates (
            tenant_id, slug, version, channel, name,
            subject_template, body_html_template, body_text_template, message_template,
            variables_schema, metadata, is_active, created_by_api_key_id
        ) VALUES (
            CAST(:tid AS uuid), :slug, :ver, :ch, :name,
            :subj, :html, :txt, :msg,
            CAST(:vs AS jsonb), CAST(:md AS jsonb), true, CAST(:akid AS uuid)
        )
        RETURNING id::text AS id, slug, version, channel, name, is_active, created_at
    """), {
        "tid": ctx.tenant_id, "slug": cur["slug"], "ver": new_version,
        "ch": cur["channel"], "name": body.name,
        "subj": body.subject_template, "html": body.body_html_template,
        "txt": body.body_text_template, "msg": body.message_template,
        "vs": json.dumps(body.variables_schema or {}),
        "md": json.dumps(body.metadata or {}),
        "akid": ctx.api_key_id,
    })).mappings().first()
    await db.commit()
    return dict(row)
