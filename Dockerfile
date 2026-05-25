# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile - inovaweb-centro-mensajes
# Multi-stage: builder instala dependencias, runtime es imagen minima.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Deps de compilacion para cryptography, psycopg, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Copiamos primero solo metadata + paquete para que cualquier cambio en
# tests/ o docs/ no invalide la capa del pip install.
COPY pyproject.toml CLAUDE.md ./
COPY app/ ./app/
RUN python -m pip install --upgrade pip setuptools wheel
# Instalar el proyecto en un prefix portable que copiaremos al runtime.
RUN python -m pip install --prefix=/install .


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001

# Solo libpq (libreria runtime de Postgres) + curl para healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 messages

# Trae lo instalado por pip desde el builder.
COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=messages:messages app/ ./app/
COPY --chown=messages:messages database/ ./database/

USER messages

EXPOSE 8001

# Healthcheck contra el endpoint publico /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1

# Workers: 2 es suficiente para un servicio I/O-bound como el centro de mensajes.
# --forwarded-allow-ips restringido a la subnet bridge de Docker; solo el
# container Caddy puede setear X-Forwarded-For.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--workers", "2", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1,172.16.0.0/12,192.168.0.0/16"]
