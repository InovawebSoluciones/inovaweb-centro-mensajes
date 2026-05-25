"""
routers/tracking_router.py
==========================
Tracking de aperturas (pixel transparente) y clicks (redireccion) para correo.

  GET /v1/track/email/open/{message_id}        responde pixel 1x1 GIF, registra evento opened
  GET /v1/track/email/click/{message_id}?u=... redirige 302 a u, registra evento clicked

Endpoints publicos sin auth (los destinatarios no tienen API key). El message_id
sirve de token efímero. NO se expone informacion del destinatario en respuesta.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/track/email", tags=["tracking"])


# Pixel GIF 1x1 transparente (43 bytes).
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


@router.get("/open/{message_id}")
async def track_open(message_id: UUID, request: Request) -> Response:
    """Pixel 1x1. Registra evento opened. Siempre devuelve el pixel (no leak)."""
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:500]

    async with SessionLocal() as db:
        msg = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, tracking_open
            FROM messages WHERE id = CAST(:mid AS uuid) LIMIT 1
        """), {"mid": str(message_id)})).mappings().first()

        # Si el mensaje no existe o no tenia tracking activado, devolvemos
        # el pixel igual (no diferenciamos al observador).
        if msg and msg["tracking_open"]:
            try:
                await db.execute(text("""
                    INSERT INTO message_events (
                        message_id, tenant_id, event_type, ip_address, user_agent, occurred_at
                    ) VALUES (
                        CAST(:mid AS uuid), CAST(:tid AS uuid),
                        'opened', CAST(:ip AS inet), :ua, NOW()
                    )
                """), {"mid": msg["id"], "tid": msg["tenant_id"], "ip": client_ip, "ua": user_agent})
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.warning("track_open: error registrando -- %s", exc)

    return Response(
        content=_PIXEL_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, private",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/click/{message_id}")
async def track_click(
    message_id: UUID,
    request: Request,
    u: str = Query(..., max_length=2048),
) -> RedirectResponse:
    """Registra click y redirige 302 a u. Valida URL minima."""
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "URL destino invalida")

    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:500]

    async with SessionLocal() as db:
        msg = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, tracking_click
            FROM messages WHERE id = CAST(:mid AS uuid) LIMIT 1
        """), {"mid": str(message_id)})).mappings().first()

        if msg and msg["tracking_click"]:
            try:
                await db.execute(text("""
                    INSERT INTO message_events (
                        message_id, tenant_id, event_type,
                        ip_address, user_agent, url_clicked, occurred_at
                    ) VALUES (
                        CAST(:mid AS uuid), CAST(:tid AS uuid),
                        'clicked', CAST(:ip AS inet), :ua, :url, NOW()
                    )
                """), {
                    "mid": msg["id"], "tid": msg["tenant_id"],
                    "ip": client_ip, "ua": user_agent, "url": u[:2048],
                })
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.warning("track_click: error registrando -- %s", exc)

    return RedirectResponse(url=u, status_code=302)
