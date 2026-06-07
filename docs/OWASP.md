# OWASP — Auditoría de seguridad de inovaweb-centro-mensajes

Auditoría por categoría. Fecha: 2026-06-06. Verificada contra el código (se
cita archivo:línea). Complementa `SECURITY.md` (modelo de amenazas).

Veredicto por categoría: **PASS** (control adecuado), **WARN** (control parcial
o riesgo aceptado documentado), **FAIL** (vulnerabilidad activa). No se
detectaron FAIL.

| Categoría | Veredicto |
|-----------|-----------|
| SQL Injection | PASS |
| XSS / inyección en plantillas | WARN |
| CSRF | N/A (PASS por diseño) |
| Secrets y cifrado | PASS |
| Endpoints sin auth | PASS |
| Webhooks firmados | PASS |
| Tracking / open redirect | PASS |
| Rate limiting / DoS | WARN |
| Inmutabilidad / auditoría | PASS |

---

## 1. SQL Injection — PASS

Todas las consultas usan `sqlalchemy.text()` con parámetros enlazados
(`:param`), nunca interpolación de valores de usuario. Ejemplos:
`api_key_auth.py:110-121`, `messages_router.py:398-437`,
`credentials_router.py:79-94`.

Donde se construye SQL dinámicamente (filtros), sólo se concatenan **fragmentos
con nombres de columna fijos**, y los valores siempre van como parámetros
enlazados (`messages_router.py:676-699`). El `group_by` del reporte de uso se
restringe con un patrón regex y se mapea a un set cerrado de columnas, no se
interpola texto libre (`messages_router.py:764-770`).

**Veredicto: PASS.** No hay vectores de inyección detectados.

---

## 2. XSS / inyección en plantillas — WARN

**Render de plantillas (seguro).** El motor acepta sólo `{nombre_variable}`
plano vía regex acotado; rechaza acceso a atributos/subscripts/format-specs
(`template_render.py:28`, `38-62`). Esto cierra el vector
`str.format_map` previo (`{var.__class__}`). Las variables se validan contra un
schema tipado al alta y al despachar (`template_render.py:65-132`,
`messages_router.py:362-369`).

**Riesgo aceptado y documentado.** Para `origin_kind=ai_generated`, el
`body_html` provisto por el caller **no se sanitiza** como HTML
(`messages_router.py:106`, `374-377`). No es un XSS contra el servidor (el
centro es API JSON, no renderiza HTML propio): el HTML se ejecuta en el lector
de correo del destinatario. El riesgo de inyección de markup en el correo
recae en el caller que genera ese HTML. Mitigaciones presentes: CSP
`default-src 'none'` en el proxy (`Caddyfile:37`,
**[TODO: verificar config Nginx real en VPS]**), y el contenido es snapshot
inmutable (`002:101-105`).

**Veredicto: WARN.** Seguro contra el servidor; sanitización del `body_html`
de `ai_generated` queda como responsabilidad documentada del caller.

---

## 3. CSRF — N/A (PASS por diseño)

El servicio es API-to-API autenticada por header `X-API-Key`, no por cookies de
sesión. En `ENV=prod` no hay CORS habilitado (`main.py:103-110`). Sin estado de
sesión basado en cookies, CSRF no aplica. Los endpoints públicos de tracking son
GET idempotentes protegidos por firma HMAC (no mutan estado de negocio
sensible).

**Veredicto: N/A / PASS por diseño.**

---

## 4. Secrets y cifrado — PASS

- API keys del centro: SHA-256, plaintext jamás persistido
  (`api_key_auth.py:59-61`, `001:59`).
- Credenciales de proveedor: AES-256-GCM (confidencialidad + autenticidad),
  nonce nuevo por cifrado, `AES_KEY` de 32 bytes validada en arranque
  (`crypto.py:38-69`). Nunca devueltas en lectura
  (`credentials_router.py:99-116`).
- Firma de tracking derivada de `AES_KEY` (no requiere secreto extra),
  comparación timing-safe (`tracking_signing.py:37-46`, `64`).
- Redacción de tokens/keys en blobs antes de loguear/persistir
  (`ledger_client.py:176-189`).
- Logging con reglas de privacidad: nunca API keys, body del mensaje, ni
  destinatario completo (`observability.py:14-19`).
- `.env` excluido del repo (`.gitignore`); contenedor corre como no-root con
  hardening kernel (`docker-compose.yml:78-90`).

**Veredicto: PASS.** Nota operativa: `AES_KEY` es secreto raíz único — su
rotación re-cifra credenciales e invalida URLs de tracking (ver `DEPLOY.md`).

---

## 5. Endpoints sin auth — PASS

Endpoints públicos (sin `X-API-Key`):
- `/health`, `/health/db` — sin datos sensibles (`health_router.py`).
- `/docs`, `/redoc`, `/openapi.json` — **deshabilitados en `ENV=prod`**
  (`main.py:78`, `93-95`).
- `/webhooks/{provider}` — sin API key, pero **firma criptográfica
  obligatoria** del proveedor (ver §6).
- `/v1/track/email/open|click` — sin API key, pero **firma HMAC obligatoria**
  (ver §7).

Todos los endpoints de negocio exigen `require_scope(...)`
(`messages_router.py:342`, `663`, `718`, `759`; `templates_router.py:46`, etc.).
El `tenant_id` se resuelve de la key, nunca del body (anti-IDOR;
`api_key_auth.py:161-165`). El detalle de mensaje no diferencia "no existe" de
"no es tuyo" (`messages_router.py:740-742`).

**Veredicto: PASS.**

---

## 6. Webhooks firmados — PASS

`POST /webhooks/{provider}` valida la firma del proveedor antes de procesar
(`webhooks_router.py:143-164`). Para Resend: HMAC-SHA256 estilo svix sobre
`{svix-id}.{svix-timestamp}.{body}`, con **anti-replay** de ventana 5 min
(`resend.py:197-239`). Comparación timing-safe (`resend.py:236`).

Defensas adicionales:
- Tope de payload 256 KB anti-DoS (`webhooks_router.py:84-87`).
- **Cross-tenant guard**: el tenant que validó la firma debe ser dueño del
  mensaje, o se rechaza 401 (`webhooks_router.py:174-181`).
- Dedup de eventos por `UNIQUE(external_message_id, event_type)`
  (`001:327`, `webhooks_router.py:197`).
- Stubs (SendGrid/Meta/Twilio) responden 501 en vez de aceptar sin verificar
  (`webhooks_router.py:151-152`).

**Veredicto: PASS.**

---

## 7. Tracking / open redirect — PASS

`/v1/track/email/click` era un vector de redirector abierto. Mitigado:
- Firma HMAC obligatoria sobre `(message_id, url)` exacta: cambiar la URL
  invalida la firma (`tracking_signing.py:54-56`, `tracking_router.py:121-122`).
- Sólo esquemas `http/https` con netloc válido (`tracking_router.py:115-117`).
- Allowlist de dominios por tenant (`tenant_tracking_allowlist`), con match de
  dominio exacto o subdominio (`tracking_router.py:139-155`, `179-187`).
- Firma inválida / sin tracking → 404 sin diferenciar (anti-enumeración).
- El pixel `open` también exige firma (`tracking_router.py:71-72`).

**Veredicto: PASS.** Nota: si la allowlist del tenant está vacía, el modo es
permisivo (cualquier dominio), pero la firma sigue siendo obligatoria.

---

## 8. Rate limiting / DoS — WARN

- Rate limiting por API key vía Redis, **opcional**, con degradación elegante
  si Redis no está disponible (`api_key_auth.py:64-83`). En despliegues sin
  `REDIS_URL` **no hay rate limiting aplicado**.
- Cortes defensivos presentes: longitud de `X-API-Key` (`api_key_auth.py:101`),
  payload de webhook 256 KB (`webhooks_router.py:84-87`), límites de tamaño en
  campos pydantic (`messages_router.py:96-112`), límites de
  memoria/CPU del contenedor (`docker-compose.yml:88-90`).

**Veredicto: WARN.** Habilitar Redis en producción para rate limiting efectivo
(pendiente conocido).

---

## 9. Inmutabilidad / auditoría — PASS

Append-only forzado por triggers de BD, no sólo por código
(`002_security_constraints.sql`): no DELETE en `messages`/`templates`/
`api_keys`/`tenant_channel_credentials`; `message_events` no UPDATE ni DELETE;
columnas críticas inmutables; lifecycle de `status` validado. Ver ADR-002.

**Veredicto: PASS.**

---

## Resumen de acciones recomendadas

1. **WARN §8**: habilitar `REDIS_URL` en producción para rate limiting.
2. **WARN §2**: definir y documentar la política de sanitización del
   `body_html` de `ai_generated` en el caller (o añadir un sanitizador
   opcional server-side).
3. **Operativo**: completar la verificación del reverse proxy
   **[TODO: verificar config Nginx real en VPS]** (HSTS, CSP, X-Forwarded-For).
4. **Worker pendiente**: implementar `dispatch_retry` (hoy un fallo transitorio
   del proveedor deja el mensaje en `failed` sin reintento automático —
   `messages_router.py:525-536`).
