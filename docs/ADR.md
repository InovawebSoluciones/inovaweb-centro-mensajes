# Architecture Decision Records — inovaweb-centro-mensajes

Registro de decisiones de arquitectura del core Nivel 1 "Centro de Mensajes".
Cada ADR documenta una decisión firme, su contexto, las alternativas y las
consecuencias. Las decisiones aquí registradas están verificadas contra el
código (se cita archivo:línea).

Estado de los ADR: todos **Aceptados** salvo indicación contraria.
Fecha de consolidación: 2026-06-06.

| ADR | Título | Estado |
|-----|--------|--------|
| 001 | Dinero en centavos enteros (BIGINT), CHECK > 0, inmutable | Aceptado |
| 002 | Append-only por triggers de base de datos | Aceptado |
| 003 | Plantillas versionadas e inmutables | Aceptado |
| 004 | Credenciales de proveedor cifradas con AES-256-GCM | Aceptado |
| 005 | Multi-tenant estricto: tenant_id resuelto de la API key | Aceptado |
| 006 | Reporte de cobro idempotente al Finanzas-Core (ledger_retry) | Aceptado |
| 007 | Render de plantillas con sustitución plana segura | Aceptado |
| 008 | Tracking público firmado con HMAC derivado de AES_KEY | Aceptado |

---

## ADR-001 — Dinero en centavos enteros (BIGINT), CHECK > 0, inmutable

**Contexto.** El centro registra el cargo de cada despacho y lo reporta al
Finanzas-Core. Usar floats para dinero introduce errores de redondeo
acumulables e irreconciliables en un ledger consolidado.

**Decisión.** Toda cantidad monetaria se almacena como entero de centavos en
columnas `BIGINT`. Nunca floats. La columna `messages.amount_cents_charged`
es `BIGINT` (`database/001_initial_schema.sql:261`), con CHECK que obliga a
ser `NULL` o `> 0` (`database/002_security_constraints.sql:224-229`). El
catálogo de precios por canal es entero
(`app/routers/messages_router.py:70-74`: email=50, whatsapp=100, sms=150).
El cliente HTTP del ledger valida `amount_cents <= 0`
(`app/core/ledger_client.py:124-125`).

**Transición controlada.** `amount_cents_charged` admite una sola transición
`NULL → valor` (snapshot del catálogo al despachar); una vez seteado es
inmutable (`database/003_audit_fixes.sql:79-83`). En el flujo de email el
monto se setea desde el INSERT inicial
(`app/routers/messages_router.py:396-397`), evitando el problema de transición
que detectó la auditoría.

**Consecuencias.** Reconciliación exacta con el Finanzas-Core. La definición
de precios y planes NO vive aquí — eso es responsabilidad de admin-financiera
Nivel 2. El catálogo plano es un placeholder temporal.

---

## ADR-002 — Append-only por triggers de base de datos

**Contexto.** El log de despachos es la fuente de verdad para auditoría y
cobranza. Un error de código o un acceso SQL directo no debe poder borrar o
falsificar el historial.

**Decisión.** Las invariantes de inmutabilidad se fuerzan en la base de datos
con triggers `BEFORE`, no sólo en Python. La red de seguridad no depende del
desarrollador (`database/002_security_constraints.sql:5-7`):

- `messages`: prohíbe DELETE (`002:27-40`). Las columnas críticas son
  inmutables (`002:53-142`, refinado en `003:22-105`): sólo se permiten mutar
  `status`, timestamps de lifecycle, `events_count`, `last_error`, columnas
  `ledger_*`, `dispatch_*` y las transiciones únicas `NULL → valor` de
  `amount_cents_charged`, `external_message_id` y `ledger_request_id`.
- `messages`: lifecycle de `status` validado por trigger —
  `queued → sent|failed`, `sent → delivered|bounced|failed`
  (`002:154-177`).
- `templates`: prohíbe DELETE; sólo se puede togglear `is_active`, `name`,
  `metadata` (`002:273-320`).
- `api_keys`: prohíbe DELETE; invalidación vía `is_active=false` +
  `revoked_at` (`002:326-337`).
- `tenant_channel_credentials`: prohíbe DELETE; rotación vía `is_active=false`
  (`002:343-355`).
- `message_events`: append-only total — prohíbe UPDATE y DELETE
  (`002:361-378`).

**Consecuencias.** Para "revertir" un cargo se inserta una entry inversa en el
Finanzas-Core con `source_ref = "msg-<channel>-<message_id>-reversal"`; el
registro original permanece (`002:30-34`). El código de aplicación debe operar
sólo con UPDATE sobre columnas mutables; cualquier otra mutación rompe con
excepción de Postgres.

---

## ADR-003 — Plantillas versionadas e inmutables

**Contexto.** Un mensaje histórico debe poder re-leerse exactamente como fue
enviado. Si una plantilla se edita en sitio, los registros pasados quedan
inconsistentes con la auditoría.

**Decisión.** Las plantillas son inmutables una vez creadas. La unicidad es
`(tenant_id, slug, version)` (`database/001_initial_schema.sql:151`). Una
corrección NO muta la fila: `PATCH /admin/v1/templates/{id}` inserta una nueva
fila con `version += 1` basada en la referenciada; el `slug` y el `channel` no
pueden cambiar entre versiones (`app/routers/templates_router.py:136-193`,
especialmente `170` y `162-163`). El despacho carga siempre la versión activa
más alta (`app/routers/messages_router.py:206-217`,
`ORDER BY version DESC LIMIT 1`). El trigger de BD bloquea cualquier mutación
de las columnas de contenido (`database/002_security_constraints.sql:287-320`).

**Plantillas NO hardcodeadas.** No existen plantillas en código; se crean por
tenant vía `POST /admin/v1/templates`. Ver trampa documentada en
`GUIA-DESARROLLADOR.md` sobre las plantillas `caf-*` aún no sembradas.

**Consecuencias.** El versionado es append-only y auditable. El consumidor
referencia plantillas por `slug` (no por UUID); el centro resuelve la versión.

---

## ADR-004 — Credenciales de proveedor cifradas con AES-256-GCM

**Contexto.** El centro guarda secretos de proveedores externos (API keys de
Resend, tokens de Meta, etc.). Estos no deben ser legibles si la base de datos
es exfiltrada.

**Decisión.** Las credenciales se cifran con AES-256-GCM antes de tocar disco
(`app/core/crypto.py`). Formato almacenado:
`base64(nonce[12] || ciphertext || tag[16])` (`crypto.py:9-11`). La clave
`AES_KEY` se lee de entorno, debe ser 32 bytes en base64 y se valida en
arranque (fail-fast, `crypto.py:38-53`). Cada cifrado genera un nonce nuevo
(`crypto.py:65`). GCM aporta confidencialidad + autenticidad: si la BD se
altera sin la `AES_KEY`, la verificación falla y se levanta `CryptoError`
(`crypto.py:93-97`). Las credenciales JAMÁS se devuelven en endpoints de
lectura — sólo metadatos (`app/routers/credentials_router.py:99-116`).

**Consecuencias.** La `AES_KEY` es el secreto raíz del sistema (también deriva
la clave de firma de tracking, ver ADR-008). Su rotación implica re-cifrar
todas las credenciales y reemitir las URLs de tracking activas. Debe vivir sólo
en el `.env` del VPS, nunca en el repo.

---

## ADR-005 — Multi-tenant estricto: tenant_id resuelto de la API key

**Contexto.** Un consumidor no debe poder operar sobre datos de otro tenant,
ni siquiera enviando un `tenant_id` falso en el body.

**Decisión.** La autenticación es por header `X-API-Key`. Se calcula SHA-256
de la key recibida y se busca por `key_hash`
(`app/core/api_key_auth.py:59-61`, `108-121`). El `tenant_id` se resuelve
SIEMPRE de la fila de `api_keys`, nunca del body
(`api_keys` → `AuthContext.tenant_id`, `api_key_auth.py:161-165`). Cada query
filtra por `tenant_id = CAST(:tid AS uuid)` con el valor del contexto
(p.ej. `messages_router.py:676-677`, `735-738`). Hay scopes por endpoint
(`messages:write`, `messages:read`, `admin:templates`, `admin:credentials`,
`*`; `database/001_initial_schema.sql:67-73`). El plaintext de la key jamás se
guarda (`001:59`). Los endpoints de credenciales comparan tenant como UUID y
exigen scope `*` para operar cross-tenant
(`app/routers/credentials_router.py:51-53`).

**Defensa cross-tenant en webhooks.** El webhook verifica que el tenant que
validó la firma sea el dueño del mensaje, evitando que un tenant forje eventos
de otro (`app/routers/webhooks_router.py:174-181`).

**Consecuencias.** Mensajes de error unificados para "no existe / inactiva /
no es tuyo" (anti-enumeración: `api_key_auth.py:124-130`,
`messages_router.py:740-742`). El alta de tenants y API keys hoy es vía SQL
directo (pendiente diferido en CLAUDE.md §9).

---

## ADR-006 — Reporte de cobro idempotente al Finanzas-Core con worker de reintento

**Contexto.** Cada despacho exitoso debe generar un cargo en el ledger
consolidado del Finanzas-Core. La red puede caerse entre el envío y el
reporte; un reintento no debe duplicar el cargo.

**Decisión.** Al marcar un mensaje como `sent`, el centro emite
`POST /v1/ledger/entries` con `source_slug="messages"`,
`direction="debit"` y `source_ref = "msg-<channel>-<message_id>"`
(`app/core/ledger_client.py:105-154`, `source_ref_for` en `213-215`). La
idempotencia la garantiza el Finanzas-Core sobre el `source_ref`: reintentar
devuelve `idempotent_replay=true` sin duplicar (`ledger_client.py:34-36`).

Los errores se mapean a excepciones tipadas: `LedgerAuthError` (401/403, no
reintentar), `LedgerValidationError` (422, bug del centro, no reintentar),
`LedgerTransientError` (5xx/red, reintentable), `LedgerError` (fatal → manual)
(`ledger_client.py:56-69`, `156-171`).

El estado del cargo se persiste en `messages.ledger_status`
(`not_applicable | pending | recorded | failed | manual`,
`database/001_initial_schema.sql:270`). El worker `ledger_retry`
(`app/workers/ledger_retry.py`) corre cada 60 s, toma lotes de 50 con
`FOR UPDATE SKIP LOCKED` (`ledger_retry.py:42-44`, `74`), reintenta `pending`
y `failed`, y tras `MAX_ATTEMPTS=8` escala a `manual`
(`ledger_retry.py:155-158`). El loop arranca en el lifespan de FastAPI
(`app/main.py:58`).

**Consecuencias.** El despacho responde 202 sin bloquear por el ledger; el
cobro se concilia de forma asíncrona y resiliente. `SKIP LOCKED` permite varios
workers uvicorn sin doble procesamiento (`main.py:56-57`).

---

## ADR-007 — Render de plantillas con sustitución plana segura

**Contexto.** El render previo usaba `str.format_map`, que permite acceso a
atributos (`{var.__class__}`) y subscripts — vector de exfiltración hacia el
destinatario del correo.

**Decisión.** El renderizador acepta SÓLO lookups planos `{nombre_variable}`
vía regex acotado `\{([a-zA-Z_][a-zA-Z0-9_]{0,127})\}`
(`app/core/template_render.py:28`, `38-62`). Cualquier cosa con punto,
corchete, `!` o `:` se ignora literalmente. Las variables se validan contra un
`variables_schema` tipado declarado al alta de la plantilla (tipos:
string/integer/number/boolean/array/object;
`template_render.py:31`, `65-132`). La validación se aplica tanto al crear la
plantilla (`templates_router.py:51-54`) como al despachar
(`messages_router.py:362-369`).

**Consecuencias.** No hay evaluación de expresiones ni acceso a atributos. Ver
ADR/OWASP: el contenido renderizado de `origin_kind=ai_generated`
(`body_html`) NO se sanitiza HTML — se ejecuta en el lector de correo del
destinatario, no en el servidor (riesgo aceptado documentado en `OWASP.md`).

---

## ADR-008 — Tracking público firmado con HMAC derivado de AES_KEY

**Contexto.** Los endpoints de tracking (`/v1/track/email/open|click`) son
públicos: los destinatarios no tienen API key. Sin protección, cualquiera
podría enumerar message_ids o usar `/click?u=` como redirector abierto
(phishing-as-a-service).

**Decisión.** Cada URL de tracking lleva un parámetro `sig` obligatorio:
HMAC-SHA256 truncado a 128 bits, base64url sin padding
(`app/core/tracking_signing.py:42-46`). La clave de firma se deriva de
`AES_KEY` con un label fijo (HKDF-light), sin variable de entorno adicional
(`tracking_signing.py:37-39`). Para OPEN se firma `open|{message_id}`; para
CLICK se firma `click|{message_id}|{url}`, de modo que la firma no sirve para
redirigir a otra URL (`tracking_signing.py:49-56`). La comparación es
timing-safe (`hmac.compare_digest`, `tracking_signing.py:64`, `72`). Firma
inválida → 404 sin diferenciar de "no existe"
(`app/routers/tracking_router.py:71-72`, `121-122`). Adicionalmente, los
clicks aplican una allowlist de dominios por tenant
(`tenant_tracking_allowlist`, `tracking_router.py:139-155`).

**Consecuencias.** El secreto de firma comparte ciclo de vida con `AES_KEY`
(rotarla invalida las URLs de tracking ya emitidas). El modo allowlist es
permisivo si la tabla está vacía para el tenant (firma sigue siendo
obligatoria).
