"""
routers/tracking_router.py
==========================
Tracking de aperturas (pixel transparente) y clicks (redireccion) para correo.

  GET /v1/track/email/open/{message_id}?sig=...
  GET /v1/track/email/click/{message_id}?u=...&sig=...

Endpoints publicos sin auth (los destinatarios no tienen API key). Sin embargo:

  - El parametro `sig` es OBLIGATORIO: HMAC de (message_id) o (message_id|url)
    con secreto derivado del AES_KEY (ver app/core/tracking_signing.py). Esto
    cierra los siguientes vectores que la auditoria detecto:
      1. Open redirect / phishing-as-a-service en /click?u=<URL maligna>.
      2. Enumeracion de message_id existentes (timing / status code distintos).

  - Para clicks adicionalmente se aplica la allowlist por tenant
    (tabla `tenant_tracking_allowlist`). Si el tenant tiene rows en esa tabla,
    el dominio destino DEBE estar listado; si la tabla esta vacia para ese
    tenant, se acepta cualquier dominio (modo permisivo con firma protegida).
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
from app.core.tracking_signing import verify_click, verify_open

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/track/email", tags=["tracking"])


# Pixel GIF 1x1 transparente (43 bytes).
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


def _pixel_response() -> Response:
    return Response(
        content=_PIXEL_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, private",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/open/{message_id}")
async def track_open(
    message_id: UUID,
    request: Request,
    sig: str = Query(..., max_length=64),
) -> Response:
    """Pixel 1x1. Requiere firma valida. Si invalida, 404 (no diferencia de no-existe)."""
    message_id_str = str(message_id)

    # Validar firma ANTES de cualquier I/O (defensa en profundidad).
    if not verify_open(message_id_str, sig):
        raise HTTPException(404, "not found")

    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:500]

    async with SessionLocal() as db:
        msg = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, tracking_open
            FROM messages WHERE id = CAST(:mid AS uuid) LIMIT 1
        """), {"mid": message_id_str})).mappings().first()

        if not msg or not msg["tracking_open"]:
            # No diferenciamos al observador: devolvemos el pixel igual.
            return _pixel_response()

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

    return _pixel_response()


@router.get("/click/{message_id}")
async def track_click(
    message_id: UUID,
    request: Request,
    u: str = Query(..., max_length=2048),
    sig: str = Query(..., max_length=64),
) -> RedirectResponse:
    """Registra click y redirige 302 a u. Firma obligatoria + allowlist por tenant."""
    message_id_str = str(message_id)

    # 1. Validar URL minima.
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(404, "not found")

    # 2. Validar firma — DEBE ser firma de (message_id, url) exacta. Si
    #    cambian uno de los dos, la firma no valida -> 404.
    if not verify_click(message_id_str, u, sig):
        raise HTTPException(404, "not found")

    # 3. Lookup mensaje + allowlist.
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:500]

    async with SessionLocal() as db:
        msg = (await db.execute(text("""
            SELECT id::text AS id, tenant_id::text AS tenant_id, tracking_click
            FROM messages WHERE id = CAST(:mid AS uuid) LIMIT 1
        """), {"mid": message_id_str})).mappings().first()

        if not msg or not msg["tracking_click"]:
            # 404 sin diferenciar: el atacante con firma valida solo puede saber
            # que SU URL pre-firmada apunta a mensaje sin tracking de click.
            raise HTTPException(404, "not found")

        # Allowlist por tenant (opcional). Si hay rows, solo dominios listados.
        target_domain = parsed.netloc.lower().rstrip(".")
        allowlist_rows = (await db.execute(text("""
            SELECT domain
            FROM tenant_tracking_allowlist
            WHERE tenant_id = CAST(:tid AS uuid) AND is_active = true
        """), {"tid": msg["tenant_id"]})).mappings().all()

        if allowlist_rows:
            allowed_domains = {r["domain"].lower().rstrip(".") for r in allowlist_rows}
            # Match exacto o subdominio de cualquiera de los listados.
            if not _matches_allowlist(target_domain, allowed_domains):
                logger.warning(
                    "track_click: dominio %s no esta en allowlist del tenant %s",
                    target_domain, msg["tenant_id"],
                )
                raise HTTPException(404, "not found")

        # Registrar evento (no bloquear redireccion si el INSERT falla).
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


def _matches_allowlist(domain: str, allowed: set[str]) -> bool:
    """True si `domain` es uno de los allowed o subdominio de uno de ellos."""
    domain = domain.lower().rstrip(".")
    if domain in allowed:
        return True
    for allowed_domain in allowed:
        if domain.endswith("." + allowed_domain):
            return True
    return False
