"""
observability.py
================
Middleware y logging estructurado JSON-lines para el Centro de Mensajes.

Componentes:
  - JsonFormatter            formatter de stdlib logging que emite JSON.
  - RequestIdMiddleware      asigna/propaga X-Request-Id; setea ContextVar.
  - configure_logging()      reemplaza handlers para emitir JSON-lines.

Filosofia: cada linea de log es JSON una-linea con timestamp, level,
request_id, path, status, latency_ms, msg + extras pasados via logger.info(...,
extra={...}). Loki / ELK / Datadog lo consumen sin pre-procesar.

Reglas de privacidad:
  - JAMAS loguear API keys (centro o proveedores).
  - JAMAS loguear el body completo del mensaje (subject, body_html, body_text,
    message_text). Solo metadatos (tenant, message_id, channel, status).
  - JAMAS loguear destinatario completo (email/telefono). Solo abreviado.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Emite cada record como JSON-line. Incluye request_id si esta en contexto."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Reemplaza handlers del root logger con JsonFormatter. Idempotente."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # Silenciar uvicorn.access (registramos via middleware).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    1. Lee X-Request-Id o genera uuid4.
    2. Setea ContextVar para que logs hijos lo incluyan.
    3. Propaga el header en la respuesta.
    4. Loggea linea de acceso al terminar el request (excepto /health).
    """

    HEADER = "x-request-id"

    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get(self.HEADER) or uuid.uuid4().hex
        token = request_id_var.set(req_id)

        start = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers[self.HEADER] = req_id
            return response
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            if request.url.path not in ("/health", "/health/db"):
                logging.getLogger("messages.access").info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_code,
                        "latency_ms": elapsed_ms,
                        "client_ip": request.client.host if request.client else None,
                    },
                )
            request_id_var.reset(token)
