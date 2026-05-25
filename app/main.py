"""
main.py
=======
Punto de entrada FastAPI del Centro de Mensajes.

Responsabilidades:
  - Construye la app FastAPI con metadatos OpenAPI.
  - Monta los routers: health (publico), messages/templates/credentials
    (autenticados via X-API-Key), webhooks (firmados por proveedor), tracking
    (publicos sin auth pero validan message_id).
  - Configura CORS para que apps cliente puedan llamar desde browser si aplica.
  - Configura logging JSON-lines segun LOG_LEVEL del .env.
  - Arranca el worker ledger_retry como tarea de fondo via lifespan.

Para correr en local:
    uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

En Docker / produccion el comando equivalente vive en el Dockerfile.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.ledger_client import shutdown_ledger_client
from app.core.observability import RequestIdMiddleware, configure_logging
from app.routers.credentials_router import router as credentials_router
from app.routers.health_router import router as health_router
from app.routers.messages_router import router as messages_router
from app.routers.templates_router import router as templates_router
from app.routers.tracking_router import router as tracking_router
from app.routers.webhooks_router import router as webhooks_router
from app.workers.ledger_retry import retry_loop_forever


configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── Startup ─────────────────────────────────────────────────────────────
    logger.info(
        "Centro de Mensajes iniciado | env=%s | finanzas=%s | public=%s",
        settings.env,
        settings.finanzas_base_url,
        settings.public_base_url,
    )
    # Worker de reintento al finanzas-core. Cada uvicorn worker corre su propio
    # loop; el SELECT con SKIP LOCKED evita doble procesamiento.
    app.state.ledger_retry_task = asyncio.create_task(retry_loop_forever())

    try:
        yield
    finally:
        # ── Shutdown ────────────────────────────────────────────────────────
        logger.info("Centro de Mensajes deteniendo...")
        task = getattr(app.state, "ledger_retry_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await shutdown_ledger_client()


app = FastAPI(
    title="Inovaweb Centro de Mensajes",
    description=(
        "Core nivel 1 de la arquitectura Inovaweb. Despacha mensajes multi-canal "
        "(Email, WhatsApp, SMS) abstraendo proveedores externos (Resend, "
        "Meta Cloud API, Twilio), mantiene catalogo central de plantillas "
        "versionadas, registra cada despacho con metadatos auditables y reporta "
        "el cobro consolidado al Finanzas-Core (https://finanzas.inovaweb.com.mx). "
        "Autenticacion por X-API-Key, multi-tenant estricto, append-only en "
        "tablas criticas. Diferencia explicitamente correo origin_kind=template "
        "vs origin_kind=ai_generated."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


# ── CORS ──────────────────────────────────────────────────────────────────────
# Permisivo: el centro es API-to-API y se filtra arriba (Caddy + API key).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(RequestIdMiddleware)


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(health_router)        # /health, /health/db
app.include_router(messages_router)      # /v1/messages/*, /v1/reports/usage
app.include_router(templates_router)     # /admin/v1/templates
app.include_router(credentials_router)   # /admin/v1/tenants/{id}/channels/{ch}/credentials
app.include_router(webhooks_router)      # /webhooks/<provider>
app.include_router(tracking_router)      # /v1/track/email/open|click
