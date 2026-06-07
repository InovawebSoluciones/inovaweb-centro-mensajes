# Reporte de Auditoría — inovaweb-centro-mensajes (Core Nivel 1)

**Fecha:** 2026-06-06 · **Capa:** Nivel 1 (core API-only) · **Rol:** notificaciones email + WhatsApp + SMS por plantillas

## Preámbulo

Se realizó una **auditoría exhaustiva de la plataforma Inovaweb** (6 proyectos),
cubriendo revisión de código, base de datos, seguridad OWASP y **consistencia de
contratos de integración entre proyectos**. Este documento transfiere al equipo del
**Centro de Mensajes** los resultados que le afectan directamente y la documentación
formal generada en esta sesión.

## 1. Veredicto del módulo

| Dimensión | Resultado |
|---|---|
| Dinero en centavos BIGINT (sin floats) | ✅ PASS (`messages.amount_cents_charged`, CHECK >0, inmutable) |
| Append-only por triggers SQL | ✅ PASS (`database/002`: messages, templates, api_keys, credentials, message_events) |
| Auth + cifrado | ✅ PASS (API key SHA-256 + scopes; credenciales de proveedor AES-256-GCM) |
| Multi-tenant | ✅ PASS (`tenant_id` desde la key, no del body) |
| Webhooks firmados | ✅ PASS (Resend svix; cross-tenant guard) |
| Tracking | ✅ PASS (HMAC anti open-redirect, allowlist por tenant) |
| OWASP | ✅ PASS (0 FAIL; varios WARN) |

## 2. Afectaciones de este módulo

### 2.1 🔴 Bloqueo operativo para el CAF — plantillas `caf-*` no sembradas
Las plantillas que el CAF enviará (`caf-pago-confirmado`, `caf-activacion-correo`,
`caf-activacion-otp`) **no existen** en el Centro de Mensajes. Las plantillas se
crean **por tenant** vía `POST /admin/v1/templates`; si no se siembran,
`POST /v1/messages/email` con `origin_kind=template` devuelve **404**
(`messages_router.py:218-222`).
**Acción:** sembrar esas plantillas para el tenant `inovaweb` antes de que el CAF
dispare correos de activación/confirmación.

### 2.2 🟠 WhatsApp y SMS devuelven 501 (afecta al CAF)
`POST /v1/messages/whatsapp` y `/v1/messages/sms` responden **501 (no implementado)**
(`messages_router.py:613-657`). El CAF asume el canal WhatsApp en su cliente
(`messages_client.py:104`, marcado TODO). Mientras no se implemente Meta Cloud API /
Twilio, **no hay canal WhatsApp/SMS** disponible.

### 2.3 WARN propios (no bloqueantes)
- **Rate limiting Redis opcional:** sin `REDIS_URL` **no se aplica** (`api_key_auth.py:64-83`).
- **`body_html` sin sanitizar** en `origin_kind=ai_generated` (`messages_router.py:106,374-377`);
  el riesgo recae en el lector de correo, no en el servidor, pero conviene sanitizar.
- **Sin worker `dispatch_retry`:** un fallo transitorio del proveedor deja el mensaje
  en `failed` sin reintento automático (`messages_router.py:525-536`); solo existe
  `ledger_retry` (hacia finanzas-core).
- **AES_KEY = secreto raíz único:** cifra credenciales **y** deriva la firma de
  tracking; rotarla re-cifra credenciales e **invalida las URLs de tracking** emitidas.

### 2.4 Reverse proxy (Caddy → Nginx)
El repo aún trae `Caddyfile` y servicio `caddy` (perfil edge); la realidad es Nginx.
En RUNBOOK/DEPLOY se dejó el marcador `[TODO: verificar config Nginx real en VPS]`.

## 3. Qué se entregó/cambió en este módulo

Documentación formal nueva (no se modificó código):
- ✅ `docs/ADR.md` (8 ADR con cita archivo:línea)
- ✅ `docs/RUNBOOK.md` (messages-api, Postgres, worker ledger_retry, webhooks, tracking, proxy)
- ✅ `docs/DEPLOY.md` (deploy, migraciones 001/002/003, rollback, checklist)
- ✅ `docs/OWASP.md` (9 categorías, veredicto PASS/WARN)
- ✅ `docs/GUIA-DESARROLLADOR.md`
- ✅ `CHANGELOG.md` (nuevo)
- ✅ `CLAUDE.md` (nota de estado fechada 2026-06-06; contenido previo intacto)

## 4. Pendientes para el equipo Centro de Mensajes

1. **Sembrar las plantillas `caf-*`** para el tenant `inovaweb` (desbloquea correos del CAF).
2. **Implementar WhatsApp (Meta Cloud API) y SMS (Twilio)** — hoy 501.
3. Implementar **worker `dispatch_retry`** para reintentos transitorios del proveedor.
4. **Activar Redis** para rate limiting real.
5. (Recomendado) sanitizar `body_html` de `ai_generated`.
6. Definir **plan de rotación de `AES_KEY`** consciente de que invalida URLs de tracking.
7. Verificar/replicar en **Nginx** los headers de seguridad y límites de body.

## 5. Commit

```bash
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-centro-mensajes" && git add . && git commit -m "docs: auditoria global + documentacion completa 2026-06-06" && git push origin main
```
