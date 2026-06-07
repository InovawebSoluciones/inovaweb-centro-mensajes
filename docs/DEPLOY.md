# DEPLOY — inovaweb-centro-mensajes

Guía de despliegue del core Nivel 1 "Centro de Mensajes".
Fecha: 2026-06-06. Verificado contra `docker-compose.yml`, `Dockerfile`,
`.env.example` y `database/00{1,2,3}_*.sql`.

Destino: VPS Contabo `89.116.25.222`, ruta `/opt/inovaweb-centro-mensajes`,
puerto host `127.0.0.1:8005` → contenedor `8001` (`docker-compose.yml:64-71`).

---

## 1. Pre-requisitos

- Docker + Docker Compose v2 en el VPS.
- Red externa `n8n_default` existente (declarada `external: true`,
  `docker-compose.yml:136-139`). Si no existe, créala o ajusta el compose.
- Reverse proxy **Nginx** configurado para `https://mensajes.inovaweb.com.mx`.
  > **[TODO: verificar config Nginx real en VPS]** — Nginx reemplazó a Caddy.
  > Verificar el `server {}` que hace `proxy_pass` al contenedor
  > `centro_mensajes:8001` (o a `127.0.0.1:8005`), con TLS y los headers de
  > seguridad del `Caddyfile` de referencia. El `Caddyfile` y el servicio
  > `caddy` del compose (perfil `edge`, `docker-compose.yml:108-124`) quedan
  > sólo como referencia.
- `.env` poblado (NO commitear). Variables obligatorias:

| Variable | Obligatoria | Notas |
|----------|-------------|-------|
| `DATABASE_URL` | sí | el compose la sobreescribe apuntando al postgres interno (`docker-compose.yml:61-63`) |
| `AES_KEY` | sí | 32 bytes en base64; secreto raíz (cifra credenciales + deriva firma de tracking) |
| `POSTGRES_PASSWORD` | sí | requerido o el compose falla (`docker-compose.yml:24`) |
| `POSTGRES_USER` | no | default `messages` |
| `POSTGRES_DB` | no | default `centro_mensajes` |
| `ENV` | sí | `dev\|staging\|prod`; sin default por diseño (`config.py:49`) |
| `FINANZAS_BASE_URL` | sí | default `https://finanzas.inovaweb.com.mx` |
| `FINANZAS_API_KEY` | sí | scope `ledger:write`, etiqueta `core-messages` |
| `PUBLIC_BASE_URL` | sí | `https://mensajes.inovaweb.com.mx` (para URLs de tracking) |
| `LOG_LEVEL` | no | default `INFO` |
| `REDIS_URL` | no | opcional; sin él el rate limiting se omite (degradación elegante) |

Generar `AES_KEY`:
```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

> En `ENV=prod` se ocultan `/docs`, `/redoc`, `/openapi.json` y se desactiva
> CORS (`app/main.py:78-110`). Verificar que `ENV=prod` antes de exponer.

---

## 2. Despliegue con Docker Compose

```bash
cd /opt/inovaweb-centro-mensajes
git pull
# revisar .env (AES_KEY, FINANZAS_API_KEY, ENV=prod, PUBLIC_BASE_URL)
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100 centro_mensajes
```

Verificación post-arranque:
```bash
# liveness
docker compose exec centro_mensajes curl -fsS http://localhost:8001/health
# readiness (BD)
docker compose exec centro_mensajes curl -fsS http://localhost:8001/health/db
```

Build: multi-stage; instala dependencias exactas de `requirements.lock`
(reproducible) y corre como usuario no-root `messages` (uid 10001)
(`Dockerfile:24-54`). El contenedor corre con `read_only: true`, `cap_drop:
ALL`, `no-new-privileges` y límites de memoria/CPU
(`docker-compose.yml:78-90`).

---

## 3. Migraciones SQL (orden 001 → 002 → 003)

**Primer arranque (BD nueva).** Postgres ejecuta automáticamente todos los
scripts de `./database` montados en `/docker-entrypoint-initdb.d`, en orden
alfabético, **una sola vez** (`docker-compose.yml:29-31`, `001:25-27`). No hay
acción manual: con el volumen `messages_pg_data` vacío, al `docker compose up`
se aplican `001`, `002` y `003`.

Contenido por archivo:
- `001_initial_schema.sql` — tablas `tenants`, `api_keys`, `message_providers`
  (sembrado de los 4 proveedores), `templates`, `tenant_channel_credentials`,
  `messages`, `message_events`, `tenant_tracking_allowlist`, índices y triggers
  de `updated_at`/`events_count`.
- `002_security_constraints.sql` — triggers append-only e inmutabilidad,
  validación de lifecycle de `status`, CHECKs de enums/formatos.
- `003_audit_fixes.sql` — refina el trigger de inmutabilidad para permitir
  `NULL → valor` en `amount_cents_charged`; añade `dispatch_attempts` y
  `last_dispatch_error`; índice de retry; tabla de allowlist (idempotente,
  `003:12-13`).

**BD existente (aplicar 003 a mano).** Las migraciones NO se re-ejecutan en
reinicios. Para aplicar `003` (o re-verificar) sobre una BD ya inicializada:
```bash
docker compose exec -T postgres psql -U messages centro_mensajes < database/003_audit_fixes.sql
```
`003` es idempotente y seguro sobre BD viva.

**Bootstrap de datos mínimos (vía SQL directo — pendiente de endpoint admin).**
El alta de tenants y API keys hoy es manual (CLAUDE.md §9). Tras inicializar:
1. Insertar el tenant en `tenants`.
2. Insertar la API key (guardando sólo `key_hash` = SHA-256 del plaintext) con
   sus `scopes`.
3. Registrar credenciales de proveedor vía
   `POST /admin/v1/tenants/{id}/channels/{channel}/credentials` (se cifran
   AES-256-GCM).
4. Sembrar plantillas vía `POST /admin/v1/templates`. **Las plantillas que
   enviará el CAF (`caf-pago-confirmado`, `caf-activacion-correo`,
   `caf-activacion-otp`) NO existen aún**: hay que crearlas o
   `POST /v1/messages/email` con `origin_kind=template` devolverá **404**
   (`messages_router.py:218-222`). Ver `GUIA-DESARROLLADOR.md`.

---

## 4. Rollback

El servicio es stateless; el estado vive en Postgres (append-only).

```bash
cd /opt/inovaweb-centro-mensajes
git checkout <tag-o-commit-anterior>
docker compose up -d --build
```

Consideraciones:
- **No hay down-migrations.** El esquema es append-only por diseño; revertir el
  código a una versión anterior es seguro mientras el esquema sea
  retro-compatible (las columnas nuevas de `003` son aditivas).
- **No borrar datos para "revertir" un cargo.** El DELETE está bloqueado por
  trigger (`002:27-40`); la reversión contable se hace en el Finanzas-Core con
  `source_ref ...-reversal` (ADR-002).
- Backup antes de cualquier cambio de esquema:
  `docker compose exec postgres pg_dump -U messages centro_mensajes > backup.sql`.

---

## 5. Checklist de deploy

- [ ] `.env` con `ENV=prod`, `AES_KEY` (32B base64), `FINANZAS_API_KEY`,
      `PUBLIC_BASE_URL` correctos. `.env` NO en git (`.gitignore`).
- [ ] Red externa `n8n_default` existe.
- [ ] **[TODO: verificar config Nginx real en VPS]** proxy a `8001`/`8005` con
      TLS y headers de seguridad; `X-Forwarded-For` desde IP permitida
      (`Dockerfile:70`).
- [ ] `docker compose up -d --build` sin errores; ambos contenedores `healthy`.
- [ ] `GET /health` y `GET /health/db` → 200.
- [ ] En BD nueva: triggers append-only presentes
      (`information_schema.triggers`); 4 proveedores sembrados en
      `message_providers`.
- [ ] En BD existente: `003` aplicado (`SELECT 'OK 003 applied'`).
- [ ] Tenant, API key (hash), credenciales de proveedor y **plantillas `caf-*`**
      sembradas.
- [ ] Smoke test: `POST /v1/messages/email` con plantilla real → 202; el
      mensaje aparece en `GET /v1/messages`; `ledger_status` avanza a
      `recorded`.
- [ ] `/docs` NO accesible públicamente (sólo si `ENV=prod`).
- [ ] Verificar webhook de Resend configurado con `webhook_secret` y apuntando
      a `https://mensajes.inovaweb.com.mx/webhooks/resend`.
