# Modelo de seguridad - inovaweb-centro-mensajes

Core multi-canal de mensajeria. Criticidad ALTA - maneja credenciales de
proveedores comerciales (Resend, Twilio, Meta), contenidos personales de
usuarios finales y conteo contable. Comprometer este servicio = poder
enviar mensajes en nombre de cualquier tenant, leer historial de
comunicaciones y alterar el cobro al cliente.

---

## 1. Activos y superficies

| Activo | Sensibilidad | Donde vive |
|---|---|---|
| `tenant_channel_credentials` (API keys de Resend/Twilio/Meta cifradas) | Critico | Postgres, AES-256-GCM |
| `messages` (registros de envios con destinatarios y contenidos) | Critico | Postgres, append-only enforced |
| `templates` (plantillas con variables tipadas) | Medio | Postgres |
| API keys del propio centro | Critico | `api_keys.key_hash` SHA-256 |
| `AES_KEY` master | Critico | `.env` |
| Key del centro al finanzas-core (scope ledger:write) | Critico | `.env` (`FINANZAS_API_KEY`) |

Endpoints expuestos a internet via Caddy:
- `POST /v1/messages/{channel}` - emisores (X-API-Key + scope `messages:write`)
- `GET /v1/messages/*` - consumidores propios y financiera
- `POST /admin/v1/*` - administracion central
- `POST /webhooks/{provider}` - publicos, firmados por el proveedor
- `GET /v1/track/email/*` - publicos, recibe pixeles y clicks de destinatarios
- `GET /health` `/docs` `/openapi.json` - publicos sin secretos

---

## 2. Controles aplicados

### 2.1 Integridad de mensajes
- `messages`: DELETE bloqueado por trigger `trg_messages_block_delete`.
- UPDATE de columnas criticas bloqueado por `trg_messages_block_mutation`
  (id, tenant_id, app_id, client_id, channel, origin_kind, to_destination,
  sent_at, external_message_id, amount_cents_charged).
- Solo `status`, `delivered_at`, `events_count`, `last_error` pueden modificarse.
- Reversal contable = compensacion en finanzas-core con `source_ref` de patron
  `msg-<channel>-<message_id>-reversal`. NUNCA borrar el registro original.

### 2.2 Cifrado de credenciales de proveedores
- `tenant_channel_credentials.encrypted_value` cifrado con AES-256-GCM.
- Descifrado solo en memoria al momento del despacho, jamas persistido en plaintext.
- Si la BD se compromete sin la `AES_KEY` del proceso, las credenciales no se leen.
- `AES_KEY` se valida al arranque (debe ser 32 bytes base64); fail-fast si invalida.

### 2.3 Multi-tenant strict
- `tenant_id` se resuelve siempre de la X-API-Key, jamas del body.
- Toda query SQL incluye `WHERE tenant_id = CAST(:cid AS uuid)`.
- `GET /v1/messages/{id}` filtra por `(id AND tenant_id)` - 404 si cross-tenant
  attempt (no diferencia de "no existe").

### 2.4 Idempotencia y replay
- `message_id` UUID server-side garantiza unicidad sin colaboracion del cliente.
- `source_ref` al ledger es determinista (`msg-<channel>-<message_id>`), permite
  reintentar el POST al finanzas-core sin duplicar cargos.
- Webhooks entrantes deduplicados por `external_message_id` + `event_type`.

### 2.5 Autenticacion / Autorizacion
- SHA-256 hash de API keys. Plaintext jamas en BD ni en logs.
- Mensaje unificado para "key inexistente / inactiva / expirada" - previene
  enumeracion.
- Scopes minimos por endpoint:
  - `messages:write` para POST a `/v1/messages/{channel}`.
  - `messages:read` para GET propio del tenant.
  - `messages:read:financial` para admin-financiera (no permite envio).
  - `admin:templates`, `admin:credentials` para administracion central.
  - `*` para admin master.
- Limite 200 chars en header X-API-Key para evitar DoS via SHA-256.

### 2.6 Validacion de input
- `pydantic.BaseModel` con `Field` validators:
  - Telefonos en formato E.164 validados via regex `^\+[1-9]\d{6,14}$`.
  - Email validado via `email-validator`.
  - `channel` in (`email`, `whatsapp`, `sms`) cerrado.
  - `origin_kind` in (`template`, `ai_generated`) cerrado para email.
  - Variables de plantilla validadas contra el schema declarado.
  - Body HTML max 200 KB para evitar DoS y abuso de SMTP.
  - `subject` max 200 chars.

### 2.7 Validacion de webhooks entrantes
- Resend: firma svix (`svix-id`, `svix-timestamp`, `svix-signature`).
- Meta: firma HMAC SHA-256 con app secret (`X-Hub-Signature-256`).
- Twilio: firma HMAC SHA-1 con auth token (`X-Twilio-Signature`).
- Rechazar antes de procesar si firma invalida o timestamp fuera de ventana.

### 2.8 Tracking pixels y links
- `GET /v1/track/email/open/{message_id}` valida que el `message_id` exista,
  devuelve pixel 1x1 GIF transparente, registra el evento `opened`.
- `GET /v1/track/email/click/{message_id}?u=...` valida URL destino contra
  allowlist por tenant, registra evento `clicked`, redirige 302.
- No exponer informacion del destinatario en respuesta.

### 2.9 Cifrado / Secretos
- `.env` excluido de git via `.gitignore`.
- Postgres NO expone puerto a internet.
- `AES_KEY` rotable solo via procedimiento controlado (re-encrypt de credenciales).
- `FINANZAS_API_KEY` rotable sin downtime (carga al arranque).

### 2.10 Observabilidad
- `RequestIdMiddleware` asigna/propaga `X-Request-Id`.
- Logs JSON-lines con `ts, level, logger, request_id, msg + extras`.
- Access log estructurado al cierre de cada request (excepto `/health`).
- Logs nunca incluyen: API keys, credenciales de proveedores, cuerpo de mensajes,
  destinatarios completos. Solo metadatos.

### 2.11 Hardening de transporte (Caddy del stack n8n)
- HSTS 2 anos con includeSubDomains preload.
- X-Frame-Options DENY, X-Content-Type-Options nosniff,
  Referrer-Policy strict-origin-when-cross-origin.
- CSP estricta para API JSON, relajada solo en `/docs` y `/redoc`.
- Permissions-Policy bloquea FLoC/geo/mic/cam.
- Body max 512 KB en Caddy (acomoda body HTML de correos).
- TLS 1.3 con Let's Encrypt auto-renovado.

---

## 3. Amenazas y mitigaciones

| Amenaza | Mitigacion |
|---|---|
| Envio de spam o phishing en nombre del tenant | Validacion de remitente contra dominio verificado por tenant, allowlist por canal |
| Robo de credenciales de Resend/Twilio | Cifrado AES-256-GCM en BD, descifrado solo en memoria, key separada del DB |
| Borrar mensajes para ocultar comunicaciones | Trigger BD bloquea DELETE en `messages` |
| Modificar registros historicos | Trigger BD bloquea UPDATE de columnas criticas |
| Cross-tenant data leak (IDOR) | tenant_id desde API key, filtros en SQL, 404 sobre lookup |
| Doble cobro por replay del webhook del proveedor | UNIQUE (external_message_id, event_type) - segundo webhook no genera segundo cargo |
| Falsificacion de webhooks | Validacion de firma obligatoria por proveedor; rechazar antes de procesar |
| SQL injection | sqlalchemy.text() con bind params; sin interpolacion de input |
| Volumetric DoS | Caddy body max 512 KB, rate limit por API key |
| Secrets en logs | API key plaintext jamas llega al logger; Caddy redacta Authorization |
| Tracking pixel usado para SSRF interno | Restriccion estricta de URL destino en click tracking (allowlist por tenant) |

---

## 4. Checklist para cambios futuros

### Agregar un canal nuevo (ej: messenger)
- [ ] INSERT en catalogo de canales en codigo (`VALID_CHANNELS`).
- [ ] Crear `app/providers/messenger.py` implementando interface `MessageProvider`.
- [ ] Crear endpoint `POST /v1/messages/messenger` en `messages_router.py`.
- [ ] Definir convencion de payload (datos requeridos del proveedor).
- [ ] Definir convencion de `source_ref` (`msg-messenger-<message_id>`).
- [ ] Definir patron de firma de webhook del proveedor.
- [ ] Documentar en CLAUDE.md y proyecto tecnico.
- [ ] Tests: registrar envio + reintento idempotente + webhook firmado.

### Agregar un proveedor alternativo para un canal existente
- [ ] Crear `app/providers/<nombre>.py` implementando interface.
- [ ] Agregar slug a tabla `tenant_channel_providers`.
- [ ] Tests: enviar con cada proveedor + recibir webhook + manejar errores.

### Nuevo endpoint
- [ ] X-API-Key obligatorio (no JWT - el centro es API-only).
- [ ] `Depends(require_scope(...))` con scope minimo necesario.
- [ ] Filtra por `tenant_id` (resuelto de la key) en toda query SQL.
- [ ] Pydantic con `Field` validators (rangos, regex, enums).
- [ ] Tests: auth fail (401), scope fail (403), happy path.

### Migracion SQL
- [ ] Sufijo `00X_descripcion.sql` con `IF NOT EXISTS` para idempotencia.
- [ ] No tocar `messages` con DROP/ALTER de columnas existentes salvo
      ADD COLUMN. La tabla es append-only - cambios destructivos requieren
      planning aparte.

---

## 5. Pendientes de seguridad

- [ ] Implementar `actor_api_key_id` automatico en INSERT (registro de quien envio cada mensaje).
- [ ] Activar Redis para rate limiting real (`REDIS_URL` en `.env`).
- [ ] Audit log separado para operaciones admin (alta de plantillas, credenciales).
- [ ] Allowlist de IPs por proveedor en Caddy para `/webhooks/{provider}`.
- [ ] Rotacion programada de API keys (procedimiento + verificacion mensual).
- [ ] Verificacion de dominios remitentes (SPF/DKIM/DMARC) antes de habilitar envio.
- [ ] Sandbox de plantillas para preview antes de publicar en produccion.
- [ ] Cifrado opcional at-rest del campo `body_html` para tenants regulados.
- [ ] Particion mensual de `messages` cuando supere ~10M rows.
