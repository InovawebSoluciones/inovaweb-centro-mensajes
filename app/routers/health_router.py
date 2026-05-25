"""
routers/health_router.py
========================
Endpoints publicos de healthcheck.

  GET /health     liveness — siempre 200 si el proceso responde.
  GET /health/db  readiness — SELECT 1 contra Postgres; 503 si falla.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: confirma que el proceso esta vivo."""
    return {"status": "ok", "service": "centro-mensajes"}


@router.get("/health/db")
async def health_db(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Readiness: confirma que la BD responde."""
    try:
        result = await db.execute(text("SELECT 1"))
        _ = result.scalar()
    except Exception as exc:
        logger.error("health_db: SELECT 1 fallo -- %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return {"status": "ok", "db": "ok"}
