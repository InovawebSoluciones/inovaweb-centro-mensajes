# Changelog

Todos los cambios notables de **inovaweb-centro-mensajes** se documentan aquí.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y el
proyecto aspira a [Versionado Semántico](https://semver.org/lang/es/).

El servicio aún está en construcción (sprint 1, scaffolding inicial), por lo que
no hay releases etiquetados todavía.

## [Sin versión] — 2026-06-06

### Added
- Documentación formal generada en la auditoría global Inovaweb:
  - `docs/ADR.md` — 8 ADR (centavos BIGINT, append-only por triggers,
    plantillas versionadas inmutables, credenciales AES-256-GCM, multi-tenant
    por API key, ledger_retry idempotente, render plano seguro, tracking
    firmado con HMAC).
  - `docs/RUNBOOK.md` — operación por componente (messages-api, Postgres,
    worker ledger_retry, webhooks, tracking, reverse proxy).
  - `docs/DEPLOY.md` — prerrequisitos, docker compose, migraciones SQL
    001→002→003, rollback y checklist.
  - `docs/OWASP.md` — auditoría por categoría con veredictos PASS/WARN.
  - `docs/GUIA-DESARROLLADOR.md` — onboarding, mapa de código, contratos,
    flujos, convenciones y trampas.
  - `CHANGELOG.md` (este archivo).

### Notes
- Auditoría sin FAIL. WARN abiertos: rate limiting requiere `REDIS_URL` en
  producción; `body_html` de `origin_kind=ai_generated` sin sanitizar
  (se ejecuta en el lector de correo, no en el servidor); falta verificar la
  config real de Nginx en el VPS (Nginx reemplazó a Caddy).
- Plantillas `caf-pago-confirmado`, `caf-activacion-correo`,
  `caf-activacion-otp` aún NO sembradas: deben crearse vía
  `POST /admin/v1/templates` o el envío con `origin_kind=template` devuelve 404.

---

## Estado base del proyecto (previo a esta entrada)

Resumen derivado de `CLAUDE.md` y del código del sprint 1.

### Added
- API FastAPI multi-canal (email/WhatsApp/SMS) abstrae proveedores externos.
- Auth `X-API-Key` con hash SHA-256 y scopes; multi-tenant estricto (tenant_id
  desde la key).
- Catálogo de plantillas versionadas e inmutables vía `/admin/v1/templates`.
- Credenciales de proveedor por tenant+canal cifradas con AES-256-GCM vía
  `/admin/v1/tenants/{id}/channels/{ch}/credentials`.
- Proveedor Resend ACTIVO para email (envío + webhooks firmados con svix,
  anti-replay 5 min); SendGrid, Meta Cloud API y Twilio como stubs.
- Webhooks firmados `POST /webhooks/{provider}` con guard cross-tenant y dedup.
- Tracking de email (pixel de apertura + redirección de click) firmado con HMAC
  derivado de `AES_KEY`, con allowlist de dominios por tenant.
- Reporte de cobro idempotente al Finanzas-Core (`source=messages`,
  `direction=debit`, `source_ref=msg-<channel>-<message_id>`).
- Worker `ledger_retry`: reintenta cargos `pending`/`failed`, escala a `manual`
  tras 8 intentos, coexiste con varios workers vía `FOR UPDATE SKIP LOCKED`.
- Esquema Postgres con triggers append-only e inmutabilidad de columnas
  críticas (migraciones 001 y 002).
- Logging estructurado JSON con `request_id` y reglas de privacidad.
- Empaquetado Docker multi-stage no-root con hardening de kernel.

### Changed / Fixed (fixes de auditoría 4-ojos, migración 003 + código)
- `WhatsApp`/`SMS` ahora devuelven 501 honesto en vez de 202 engañoso.
- Trigger de inmutabilidad permite transición `NULL → valor` en
  `amount_cents_charged` (snapshot del catálogo al despachar).
- Render de plantillas reemplaza `str.format_map` por sustitución plana segura
  (cierra exfiltración vía `{var.__class__}`).
- Validación de `variables_schema` al alta y de variables al despachar.
- Tracking exige firma HMAC (cierra open-redirect y enumeración de message_id).
- Webhooks validan firma antes de trabajo costoso y verifican tenant correcto.
- En `ENV=prod` se ocultan `/docs`, `/redoc`, `/openapi.json` y se desactiva
  CORS.
- Columnas `dispatch_attempts`/`last_dispatch_error` e índice de retry de
  ledger (incluye `pending` huérfanos).

### Pending
- Endpoint admin para crear tenants/API keys (hoy SQL directo).
- Implementación de Meta Cloud API y Twilio.
- Worker `dispatch_retry` para reintento de envíos al proveedor.
- Rate limiting con Redis (opcional, degradación elegante si no está).
- Vistas materializadas y particionamiento mensual de `messages`.
