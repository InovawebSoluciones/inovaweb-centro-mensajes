"""
api_key_auth.py
===============
Dependencia FastAPI para autenticacion via X-API-Key en el Centro de Mensajes.

Flujo:
  1. El cliente incluye el header X-API-Key: <plain key>.
  2. Se calcula SHA-256 del valor recibido.
  3. Se busca en api_keys WHERE key_hash = <sha256> AND is_active = true.
  4. Se valida expires_at si esta definido.
  5. Se valida scope requerido.
  6. Se actualiza last_used_at sin bloquear la respuesta.
  7. Se retorna el tenant_id resuelto (resolucion desde la key, NUNCA del body).

Rate limiting opcional con Redis (graceful degradation si no esta disponible).

Uso:
    from app.core.api_key_auth import get_tenant_by_api_key, require_scope

    @router.post("/v1/messages/email")
    async def send(
        ctx: AuthContext = Depends(require_scope("messages:write")),
        db: AsyncSession = Depends(get_db),
    ):
        tenant_id = ctx.tenant_id
        actor_api_key_id = ctx.api_key_id
        ...
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)


API_KEY_HEADER = "X-API-Key"
RATE_LIMIT_WINDOW = 60  # segundos


@dataclass(frozen=True)
class AuthContext:
    """Contexto de autenticacion resuelto. Se inyecta al endpoint via Depends()."""
    tenant_id: str
    api_key_id: str
    scopes: list[str]


def _hash_key(raw_key: str) -> str:
    """SHA-256 hex de la API key. Plaintext jamas se guarda."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def _check_rate_limit(request: Request, key_id: str, limit: int) -> None:
    """Rate limit via Redis (graceful degradation si no hay Redis)."""
    try:
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            return
        redis_key = f"rl:apikey:{key_id}"
        count = await redis.incr(redis_key)
        if count == 1:
            await redis.expire(redis_key, RATE_LIMIT_WINDOW)
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit excedido. Maximo {limit} requests por minuto.",
                headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Rate limit Redis no disponible: %s", exc)


async def _resolve_api_key(
    request: Request,
    x_api_key: Optional[str],
    db: AsyncSession,
    required_scope: Optional[str],
) -> AuthContext:
    """Logica comun de resolucion de API key + scope check."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Se requiere header X-API-Key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Proteccion DoS: cortar antes de invocar SHA-256.
    if len(x_api_key) > 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key invalida o no autorizada.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    key_hash = _hash_key(x_api_key)

    result = await db.execute(text("""
        SELECT
            id::text                AS key_id,
            tenant_id::text         AS tenant_id,
            is_active,
            expires_at,
            rate_limit_per_minute,
            scopes
        FROM api_keys
        WHERE key_hash = :hash
        LIMIT 1
    """), {"hash": key_hash})
    row = result.mappings().first()

    # Mensaje unificado para "no existe" e "inactiva": previene enumeracion.
    if not row or not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key invalida o no autorizada.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if row["expires_at"] is not None:
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key expirada.",
            )

    scopes: list[str] = list(row["scopes"] or [])
    if required_scope is not None and required_scope not in scopes and "*" not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Esta API key no tiene el scope requerido: '{required_scope}'.",
        )

    await _check_rate_limit(request, row["key_id"], row["rate_limit_per_minute"])

    # Actualizar last_used_at sin bloquear la respuesta.
    try:
        await db.execute(text("""
            UPDATE api_keys SET last_used_at = NOW()
            WHERE id = CAST(:key_id AS uuid)
        """), {"key_id": row["key_id"]})
        await db.commit()
    except Exception as exc:
        logger.warning("No se pudo actualizar last_used_at: %s", exc)

    return AuthContext(
        tenant_id=row["tenant_id"],
        api_key_id=row["key_id"],
        scopes=scopes,
    )


async def get_tenant_by_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Auth basica sin scope check. Para endpoints que solo requieren key valida."""
    return await _resolve_api_key(request, x_api_key, db, required_scope=None)


def require_scope(scope: str):
    """
    Factory de dependencia que exige un scope especifico.

    Uso:
        @router.post("/v1/messages/email")
        async def send(
            ctx: AuthContext = Depends(require_scope("messages:write")),
        ):
            ...
    """
    async def _check(
        request: Request,
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
        db: AsyncSession = Depends(get_db),
    ) -> AuthContext:
        return await _resolve_api_key(request, x_api_key, db, required_scope=scope)

    return _check
