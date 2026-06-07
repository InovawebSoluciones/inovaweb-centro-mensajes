# Guía del desarrollador — inovaweb-centro-mensajes

Onboarding técnico del core Nivel 1 "Centro de Mensajes". Notifica vía
email/WhatsApp/SMS por plantillas, es API-only y reporta cada despacho al
Finanzas-Core. Fecha: 2026-06-06. Verificada contra el código.

Documentos relacionados: `CLAUDE.md` (visión y convenciones), `SECURITY.md`
(modelo de amenazas), `docs/ADR.md` (decisiones), `docs/RUNBOOK.md`
(operación), `docs/DEPLOY.md`, `docs/OWASP.md`,
`docs/01-centro-mensajes-integracion-cores.md` (contrato de integración).

---

## 1. Onboarding rápido

Stack: Python 3.12 + FastAPI + uvicorn, SQLAlchemy 2 async + psycopg 3,
PostgreSQL 16, httpx async, AES-256-GCM. Empaquetado Docker.

```bash
# Local con compose (postgres + servicio)
cp .env.example .env            # poblar AES_KEY, POSTGRES_PASSWORD, ENV=dev
docker compose up -d --build

# o dev directo (requiere postgres y .env)
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload   # app/main.py:16

# tests
pytest -q
```

`AES_KEY`: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
En `ENV=dev` se exponen `/docs` y CORS abierto; en `ENV=prod` ambos se
desactivan (`app/main.py:78-110`).

---

## 2. Mapa de código

```
app/
├── main.py                 FastAPI + wiring de routers + lifespan (arranca worker)
├── core/
│   ├── config.py           pydantic-settings (lee .env)
│   ├── database.py         AsyncEngine + get_db() (sesión por request)
│   ├── api_key_auth.py     X-API-Key (SHA-256) + scopes → AuthContext(tenant_id,...)
│   ├── crypto.py           AES-256-GCM encrypt/decrypt de credenciales
│   ├── tracking_signing.py HMAC (derivado de AES_KEY) para URLs de tracking
│   ├── template_render.py  render plano seguro + validación de schema
│   ├── ledger_client.py    httpx async → Finanzas-Core (record_entry idempotente)
│   └── observability.py    logging JSON + RequestIdMiddleware
├── routers/
│   ├── health_router.py    /health, /health/db (públicos)
│   ├── messages_router.py  /v1/messages/* , /v1/reports/usage
│   ├── templates_router.py /admin/v1/templates (CRUD, versionado)
│   ├── credentials_router.py /admin/v1/tenants/{id}/channels/{ch}/credentials
│   ├── webhooks_router.py   /webhooks/{provider} (firmados)
│   └── tracking_router.py   /v1/track/email/open|click (firmados)
├── providers/
│   ├── base.py             interface MessageProvider + DispatchResult/WebhookEvent + errores
│   ├── factory.py          build_provider(slug) → instancia; supported_slugs()
│   ├── resend.py           ACTIVO (email): send_email + svix webhook
│   ├── sendgrid.py         STUB (email)
│   ├── meta_whatsapp.py    STUB (whatsapp)
│   └── twilio.py           STUB (sms)
└── workers/
    └── ledger_retry.py     loop async: reintenta cargos failed/pending al ledger

database/
├── 001_initial_schema.sql  tablas, índices, triggers updated_at/events_count, seed proveedores
├── 002_security_constraints.sql  triggers append-only + inmutabilidad + CHECKs
└── 003_audit_fixes.sql     fix amount NULL→valor, dispatch_*, índice retry, allowlist
```

---

## 3. Contratos

### 3.1 Lo que el centro EXPONE

Autenticados con `X-API-Key` + scope:
- `POST /v1/messages/email` (scope `messages:write`) — despacha correo. 202.
  Soporta `origin_kind=template` (resuelve plantilla por slug) o
  `ai_generated` (subject+body provistos) (`messages_router.py:335-464`).
- `POST /v1/messages/whatsapp`, `POST /v1/messages/sms` — **501 No
  Implementado** en sprint 1 (`messages_router.py:613-657`).
- `GET /v1/messages` (scope `messages:read`) — listado paginado multi-eje
  (app/client/service/channel/status/fechas) (`messages_router.py:662-710`).
- `GET /v1/messages/{id}` (scope `messages:read`) — detalle + eventos
  (`messages_router.py:715-753`).
- `GET /v1/reports/usage` (scope `messages:read`) — agregados por
  channel/client/app (`messages_router.py:758-793`).
- `POST/GET/PATCH /admin/v1/templates` (scope `admin:templates`) — CRUD
  versionado e inmutable (`templates_router.py`).
- `POST/GET /admin/v1/tenants/{id}/channels/{ch}/credentials`
  (scope `admin:credentials`) — registro/listado de credenciales cifradas
  (`credentials_router.py`).

Públicos:
- `GET /health`, `GET /health/db`.
- `POST /webhooks/{provider}` — firma del proveedor obligatoria.
- `GET /v1/track/email/open/{id}?sig=`, `/click/{id}?u=&sig=` — firma HMAC
  obligatoria.

### 3.2 Lo que el centro CONSUME: Finanzas-Core

`POST {FINANZAS_BASE_URL}/v1/ledger/entries` con `X-API-Key=FINANZAS_API_KEY`
(scope `ledger:write`). Body: `source_slug="messages"`,
`source_ref="msg-<channel>-<message_id>"` (idempotente), `direction="debit"`,
`amount_cents`, `currency`, `occurred_at`, `description`, `meta`
(`ledger_client.py:11-37`, `105-154`). Reintentar el mismo `source_ref` no
duplica (idempotent_replay).

---

## 4. Flujos

**Envío de email (`origin_kind=template`):**
1. Auth → `AuthContext.tenant_id` (de la key) (`api_key_auth.py:161-165`).
2. Carga la versión activa más alta de la plantilla; 404 si no existe
   (`messages_router.py:356-360`, `206-222`).
3. Valida `variables` contra `variables_schema`; 422 si falta/tipo malo
   (`messages_router.py:362-369`).
4. Renderiza subject/html/text con sustitución plana segura (`373-372`).
5. Si pidió tracking open, inyecta pixel firmado en el HTML (`381-391`).
6. INSERT `messages` en `queued` con `amount_cents_charged` snapshot
   (`398-438`).
7. Despacha vía provider (Resend); marca `sent`/`failed`; setea
   `external_message_id` (`467-582`).
8. POST inmediato al ledger; si falla transitorio queda `pending/failed` y el
   worker `ledger_retry` lo retoma (`256-330`, `599-608`).

**Conciliación de cobro:** worker cada 60 s reintenta `pending`/`failed`,
escala a `manual` tras 8 intentos (`ledger_retry.py`). Ver RUNBOOK §3.

**Eventos del proveedor:** `POST /webhooks/resend` → valida firma svix +
cross-tenant → inserta `message_events` (dedup) → actualiza status del mensaje
(`webhooks_router.py`). Ver RUNBOOK §4.

---

## 5. Convenciones firmes

- Dinero en centavos enteros BIGINT, nunca floats (ADR-001).
- Append-only por triggers; no DELETE, columnas críticas inmutables (ADR-002).
- Plantillas inmutables y versionadas; corrección = nueva versión (ADR-003).
- Credenciales AES-256-GCM, nunca devueltas en lectura (ADR-004).
- Multi-tenant estricto: `tenant_id` de la key, jamás del body (ADR-005).
- Canales cerrados en código: `email | whatsapp | sms`.
- `origin_kind` obligatorio para email: `template | ai_generated`
  (CHECK en BD, `002:193-201`).
- Teléfonos en E.164 (`messages_router.py:76`, `141-144`).
- `source_ref` al ledger: `msg-<channel>-<message_id>`.
- SQL siempre con `text()` + parámetros enlazados (nunca interpolar valores).
- Logs JSON; nunca loguear keys, body del mensaje ni destinatario completo
  (`observability.py:14-19`).

---

## 6. Trampas (gotchas)

- **Plantillas `caf-*` NO sembradas.** Las plantillas que enviará el CAF
  (`caf-pago-confirmado`, `caf-activacion-correo`, `caf-activacion-otp`) **no
  existen aún**. No están hardcodeadas: deben crearse por tenant vía
  `POST /admin/v1/templates`. Si no se siembran, `POST /v1/messages/email` con
  `origin_kind=template` devuelve **404** (`messages_router.py:218-222`).
- **WhatsApp y SMS devuelven 501.** No implementados en sprint 1
  (`messages_router.py:613-657`). Meta/Twilio son stubs que levantan
  `NotImplementedError`.
- **`body_html` de `ai_generated` no se sanitiza** (se ejecuta en el lector de
  correo, no en el servidor). Riesgo aceptado, ver `OWASP.md` §2.
- **`dispatch_retry` no tiene worker.** Un fallo transitorio del proveedor deja
  el mensaje en `failed` con `last_dispatch_error`, sin reintento automático
  (`messages_router.py:525-536`). Hay `ledger_retry`, no `dispatch_retry`.
- **Rate limiting es opcional.** Sin `REDIS_URL` no se aplica (degradación
  elegante, `api_key_auth.py:64-83`).
- **Rotar `AES_KEY` invalida todo.** Es el secreto raíz: re-cifra credenciales
  y rompe las URLs de tracking ya emitidas (firma deja de validar).
- **Migraciones sólo en el primer arranque.** Postgres no las re-aplica; `003`
  debe correrse a mano sobre BD existente (`DEPLOY.md` §3, es idempotente).
- **Alta de tenants/API keys es vía SQL directo** (no hay endpoint admin aún,
  CLAUDE.md §9). La key se guarda sólo como `key_hash` SHA-256.
- **Webhooks/tracking no llevan API key** pero exigen firma; sin
  `webhook_secret` configurado, los webhooks de Resend fallan
  (`resend.py:185-188`).
- **OneDrive / archivos "solo nube".** El repo vive en OneDrive; si un archivo
  se lee vacío/truncado, puede estar sin sincronizar localmente (no es bug del
  código).

---

## 7. Pendientes diferidos (de CLAUDE.md §9)

Endpoint admin para crear tenants/API keys; implementación de Meta Cloud API y
Twilio; worker `dispatch_retry`; rate limiting con Redis; vistas materializadas
para reportes de alto volumen; particionamiento mensual de `messages`.
