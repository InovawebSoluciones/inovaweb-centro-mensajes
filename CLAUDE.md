# inovaweb-centro-mensajes

Core de Nivel 1 que despacha, registra y cobra cada mensaje saliente
(Email, WhatsApp, SMS) que las apps de Inovaweb mandan a sus usuarios
finales. Abstrae proveedores externos (Resend, Meta Cloud API, Twilio),
mantiene catalogo central de plantillas y reporta cada despacho al
finanzas-core para cobranza consolidada.

**Estado:** en construccion (sprint 1, scaffolding inicial).

---

## 1. Arquitectura Inovaweb (3 niveles)

```
NIVEL 1 - APIs core (infraestructura)
├─ medidor             (operativo)
├─ hub-pasarelas       (operativo)
├─ finanzas-core       (operativo)
└─ centro-mensajes     (ESTE PROYECTO)

NIVEL 2 - servicios
└─ admin-financiera    (planeado)

NIVEL 3 - apps cliente
├─ WebEscolar          (single-tenant, ERP escolar)
├─ MicroFichas         (multi-tenant, video IA)
├─ Scraping            (n8n)
└─ Ecofile             (factura electronica, planeado)
```

**Que SI hace este modulo:**
- Despacha mensajes Email, WhatsApp y SMS via proveedores externos abstraidos.
- Catalogo administrable de plantillas pre-aprobadas con versionado.
- Catalogo administrable de credenciales por tenant y por canal (cifradas AES-256-GCM).
- Diferencia explicitamente correo con `origin_kind=template` vs `origin_kind=ai_generated`.
- Tracking opcional por pixel y reescritura de enlaces para correo.
- Recepcion de webhooks de proveedores con validacion de firma.
- Conteo y reporte automatico al finanzas-core (POST source=messages, direction=debit).
- Consulta multi-eje para dueño del proceso (por app, cliente, remitente, fecha, canal).
- Multi-tenant strict (filtro por API key).

**Que NO hace (eso es admin-financiera Nivel 2):**
- Definir precios por canal o por cliente.
- Aplicar planes o descuentos.
- Emitir facturas electronicas.
- Decidir cuando enviar un mensaje (eso es responsabilidad de la app cliente).

---

## 2. Stack tecnico

- **Python 3.12** + **FastAPI** + **uvicorn**
- **SQLAlchemy 2 async** + **psycopg 3 binary**
- **PostgreSQL 16** (auto-contenido en docker-compose)
- **httpx async** para llamadas a proveedores externos
- **AES-256-GCM** para credenciales cifradas en BD
- **Docker** + Caddy (TLS via stack n8n)
- **VPS Contabo** 89.116.25.222, puerto host 8005 (los 8000-8004 ocupados)

---

## 3. Endpoints previstos

### Publicos (sin auth)
- `GET  /health`              liveness
- `GET  /health/db`           readiness
- `GET  /docs` `/redoc` `/openapi.json`

### Autenticados (X-API-Key)
- `POST /v1/messages/email`              despachar correo (scope messages:write)
- `POST /v1/messages/whatsapp`           despachar WhatsApp
- `POST /v1/messages/sms`                despachar SMS
- `GET  /v1/messages`                    listado paginado con filtros multi-eje
- `GET  /v1/messages/{id}`               detalle con eventos de tracking
- `GET  /v1/reports/usage`               agregados por cliente, canal y periodo

### Admin (scope admin:templates / admin:credentials / *)
- `POST /admin/v1/templates`             alta de plantilla
- `GET  /admin/v1/templates`             listado de plantillas
- `PATCH /admin/v1/templates/{id}`       nueva version de plantilla
- `POST /admin/v1/tenants/{id}/channels/{channel}/credentials`  registrar credencial

### Webhooks
- `POST /webhooks/{provider}`            eventos async de proveedores
- `GET  /v1/track/email/open/{id}`       pixel de apertura
- `GET  /v1/track/email/click/{id}?u=`   redireccion con tracking de click

---

## 4. Convenciones firmes

- **Centavos enteros BIGINT.** Nunca floats.
- **Canales cerrados en codigo:** `email | whatsapp | sms` (Messenger/Instagram en fase posterior).
- **origin_kind para email:** `template | ai_generated` (obligatorio, sin ambigüedad).
- **Telefonos en E.164** con prefijo de pais, normalizados al ingreso.
- **source_ref al ledger:** `msg-<channel>-<message_id>` (UUID server-side).
- **Append-only:** triggers en BD bloquean DELETE y UPDATE de columnas criticas en `messages`.
- **Multi-tenant strict:** `tenant_id` resuelto SIEMPRE de la API key.
- **SHA-256** para hash de API keys.
- **AES-256-GCM** para credenciales de proveedores externos en BD.
- **Plantillas inmutables:** correcciones generan nueva version, no modifican la actual.

---

## 5. Estructura prevista

```
inovaweb-centro-mensajes/
├── app/
│   ├── main.py                       FastAPI + wiring
│   ├── core/
│   │   ├── config.py                 pydantic-settings
│   │   ├── database.py               AsyncEngine + get_db
│   │   ├── api_key_auth.py           X-API-Key + scopes
│   │   ├── crypto.py                 AES-256-GCM
│   │   ├── observability.py          JSON logging + request-id
│   │   └── ledger_client.py          httpx client al finanzas-core
│   ├── routers/
│   │   ├── health_router.py
│   │   ├── messages_router.py        POST envio + GET consulta
│   │   ├── templates_router.py       CRUD plantillas
│   │   ├── credentials_router.py     CRUD credenciales
│   │   ├── webhooks_router.py        recepcion async
│   │   └── tracking_router.py        pixel + click tracking
│   ├── providers/
│   │   ├── base.py                   interface MessageProvider
│   │   ├── resend.py                 implementacion email
│   │   ├── sendgrid.py               implementacion email alternativa
│   │   ├── meta_whatsapp.py          implementacion whatsapp
│   │   └── twilio.py                 implementacion sms
│   └── workers/
│       └── ledger_retry.py           reintento async al finanzas-core
├── database/
│   ├── 001_initial_schema.sql        tenants, api_keys, templates, credentials, messages, events
│   └── 002_security_constraints.sql  triggers append-only + CHECK
├── tests/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── Caddyfile (referencia)
├── .env.example
├── .gitignore
├── CLAUDE.md
└── SECURITY.md
```

---

## 6. Variables de entorno previstas

| variable           | obligatoria | default                |
|--------------------|-------------|------------------------|
| DATABASE_URL       | si          | -                      |
| AES_KEY            | si          | -                      |
| POSTGRES_USER      | no          | messages               |
| POSTGRES_PASSWORD  | si          | -                      |
| POSTGRES_DB        | no          | centro_mensajes        |
| PORT               | no          | 8001 (en contenedor)   |
| ENV                | si          | -                      |
| LOG_LEVEL          | no          | INFO                   |
| FINANZAS_BASE_URL  | si          | https://finanzas.inovaweb.com.mx |
| FINANZAS_API_KEY   | si          | (key con scope ledger:write) |

---

## 7. Despliegue

### VPS Contabo (puerto host 8005)
```
cd /opt/inovaweb-centro-mensajes
git pull
docker compose up -d --build
```

Caddy (stack n8n) enruta `https://mensajes.inovaweb.com.mx` -> `centro_mensajes:8001`
por nombre de contenedor en la red `n8n_default`.

---

## 8. Documentos clave

- `docs/inovaweb-centro-mensajes-proyecto-tecnico.md` - documento tecnico completo del proyecto, con diagramas mermaid y secciones para stakeholders y devs.
- `SECURITY.md` - modelo de amenazas y controles.
- Repositorio GitHub (planeado): https://github.com/InovawebSoluciones/inovaweb-centro-mensajes

---

## 9. Pendientes diferidos

- Endpoint admin para crear tenants y API keys (hoy via SQL directo).
- Implementacion Resend, Meta Cloud API, Twilio (sprint 1).
- Sistema de plantillas con variables tipadas y validacion.
- Tracking pixel y click con reescritura de enlaces.
- Canales adicionales: Messenger, Instagram (fase posterior).
- Vistas materializadas para reportes de alto volumen.
- Particionamiento mensual de `messages` cuando supere ~10M rows.
