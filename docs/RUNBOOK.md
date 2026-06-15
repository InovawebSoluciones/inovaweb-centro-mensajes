# RUNBOOK — inovaweb-centro-mensajes

Guía operacional por componente del core Nivel 1 "Centro de Mensajes".
Fecha: 2026-06-06. Verificado contra el código (se cita archivo:línea).

Stack en producción: VPS Contabo `89.116.25.222`, puerto host `127.0.0.1:8005`
→ contenedor `centro_mensajes:8001` (`docker-compose.yml:68-71`). El reverse
proxy/TLS lo termina un proxy externo.

> **[TODO: verificar config Nginx real en VPS]** — La documentación heredada y
> el `Caddyfile` del repo describen Caddy del stack n8n. En el entorno actual
> **Nginx reemplazó a Caddy** como reverse proxy de
> `https://mensajes.inovaweb.com.mx`. La configuración real de Nginx (server
> block, proxy_pass al contenedor `centro_mensajes:8001`, headers de seguridad,
> manejo de `X-Forwarded-For`) **debe verificarse directamente en el VPS** y
> documentarse aquí. El `Caddyfile` del repo queda sólo como referencia
> histórica de headers deseados.

---

## Componentes

| Componente | Tipo | Endpoint / Disparo |
|------------|------|--------------------|
| messages-api | Servicio FastAPI (uvicorn, 2 workers) | `:8001` |
| Postgres | BD (postgres:16-alpine) | red interna, sin puerto host |
| worker ledger_retry | Loop async dentro del proceso FastAPI | cada 60 s |
| webhooks | Endpoint dentro de messages-api | `POST /webhooks/{provider}` |
| tracking | Endpoint dentro de messages-api | `GET /v1/track/email/*` |
| Reverse proxy | Nginx (externo) | `:80/:443` → `:8005`/`:8001` |

---

## 1. messages-api (FastAPI + uvicorn)

**Qué es.** El proceso principal. Monta routers de health, messages, templates,
credentials, webhooks y tracking (`app/main.py:114-121`). Arranca con 2 workers
uvicorn (`Dockerfile:65-70`). En `ENV=prod` oculta `/docs`, `/redoc`,
`/openapi.json` y desactiva CORS (`main.py:78-110`).

**Healthchecks.**
- Liveness: `GET /health` → `{"status":"ok"}` siempre que el proceso responda
  (`app/routers/health_router.py:25-28`).
- Readiness: `GET /health/db` → ejecuta `SELECT 1`; 503 si la BD no responde
  (`health_router.py:31-43`).

**Comandos.**
```bash
# Estado del contenedor
docker compose ps centro_mensajes
# Logs (JSON-lines; busca level=ERROR / msg de access)
docker compose logs -f --tail=200 centro_mensajes
# Liveness desde dentro del contenedor
docker compose exec centro_mensajes curl -fsS http://localhost:8001/health
# Reinicio
docker compose restart centro_mensajes
```

**Diagnóstico.**
- 401 en todos los requests autenticados → revisar `X-API-Key`; el mensaje es
  unificado por anti-enumeración (`api_key_auth.py:124-130`).
- 403 → la key no tiene el scope requerido (`api_key_auth.py:143-147`).
- 500 al arrancar → `AES_KEY` ausente o no 32 bytes base64 (fail-fast,
  `crypto.py:38-53`); o `ENV` no seteado (`config.py:49`).
- Logs estructurados JSON con `request_id`, `path`, `status`, `latency_ms`
  (`observability.py:38-114`). NUNCA loguean API keys, body del mensaje, ni
  destinatario completo (`observability.py:14-19`).

---

## 2. Postgres

**Qué es.** `postgres:16-alpine`, contenedor `messages_postgres`, volumen
`messages_pg_data`, sin puerto publicado al host (sólo red `internal`)
(`docker-compose.yml:18-48`). Las migraciones de `./database` se ejecutan
**sólo en el primer arranque** vía `/docker-entrypoint-initdb.d`
(`docker-compose.yml:29-31`, `001:25-27`).

**Comandos.**
```bash
# psql interactivo
docker compose exec postgres psql -U messages centro_mensajes
# Verificar triggers append-only instalados
docker compose exec postgres psql -U messages centro_mensajes \
  -c "SELECT trigger_name, event_object_table FROM information_schema.triggers ORDER BY 2,1;"
# Backup lógico
docker compose exec postgres pg_dump -U messages centro_mensajes > backup_$(date +%F).sql
```

**Diagnóstico.**
- `health/db` da 503 → contenedor postgres caído o sin healthcheck OK
  (`docker-compose.yml:34-39`).
- Excepción `DELETE no permitido` / `... es inmutable` en logs → es esperado:
  un trigger append-only bloqueó una operación ilegal
  (`002_security_constraints.sql`). Investigar el código que la intentó, NO
  desactivar el trigger.
- Las migraciones NO se re-aplican en reinicios; para aplicar `003` sobre una
  BD existente, ver `DEPLOY.md` (es idempotente, `003:12-13`).

---

## 3. worker ledger_retry

**Qué es.** Loop async que vive dentro del proceso FastAPI, arrancado por el
lifespan (`app/main.py:58`). Reintenta cargos al Finanzas-Core. Corre cada
`LOOP_INTERVAL_SECONDS=60`, lotes de `BATCH_SIZE=50`, máx
`MAX_ATTEMPTS=8` (`app/workers/ledger_retry.py:42-44`). Usa
`FOR UPDATE SKIP LOCKED` para coexistir con los otros workers uvicorn
(`ledger_retry.py:74`).

**Qué procesa.** Mensajes con `ledger_status IN ('pending','failed')`,
`status IN ('sent','delivered','bounced')`, monto > 0, y último intento hace
> 60 s (`ledger_retry.py:56-75`). Tras 8 intentos → `ledger_status='manual'`
(revisión humana).

**Diagnóstico / queries útiles.**
```sql
-- Cargos pendientes de conciliar
SELECT ledger_status, COUNT(*) FROM messages GROUP BY 1;
-- Mensajes escalados a revisión manual (acción humana requerida)
SELECT id, channel, amount_cents_charged, ledger_attempts, ledger_last_error
FROM messages WHERE ledger_status='manual' ORDER BY ledger_last_attempt_at DESC;
```
- `manual` con `auth:` en `ledger_last_error` → `FINANZAS_API_KEY` revocada o
  sin scope `ledger:write` (`ledger_client.py:60-61`, `ledger_retry.py:151-153`).
- `manual` con `validation:` → bug del centro (body inválido), NO se reintenta
  (`ledger_client.py:64-65`).
- `pending` que no avanza → revisar conectividad al `FINANZAS_BASE_URL`
  (`config.py:34-37`). El loop loguea `ledger_retry batch: {...}` con contadores
  (`ledger_retry.py:198-199`).
- La idempotencia hace seguro reintentar manualmente el mismo `source_ref`
  (`ledger_client.py:34-36`).

---

## 4. webhooks (eventos de proveedor)

**Qué es.** `POST /webhooks/{provider_slug}`
(`app/routers/webhooks_router.py:76-77`). Recibe eventos async (delivered,
bounced, opened, clicked) y actualiza el lifecycle del mensaje.

**Flujo.** (1) valida que el `provider_slug` exista; (2) tope de 256 KB anti-DoS
(`webhooks_router.py:84-87`); (3) extrae `external_message_id` sin validar firma
aún (`webhooks_router.py:53-73`); (4) busca el mensaje; (5) itera credenciales
activas y valida la firma del proveedor; (6) **verifica que el tenant de la
credencial == tenant del mensaje** (defensa cross-tenant,
`webhooks_router.py:174-181`); (7) inserta evento deduplicado por
`UNIQUE(external_message_id, event_type)` (`webhooks_router.py:189-204`,
`001:327`).

**Estado de proveedores.** Sólo Resend implementa firma real (svix HMAC-SHA256
con anti-replay de 5 min, `app/providers/resend.py:174-239`). SendGrid, Meta y
Twilio son stubs → `verify_webhook_signature` levanta `NotImplementedError` y
el endpoint responde **501** (`webhooks_router.py:151-152`).

**Diagnóstico.**
- 401 "firma invalida" → secret de webhook mal configurado, o intento de
  spoofing, o tenant mismatch (`webhooks_router.py:166-181`).
- Eventos sin efecto → posible duplicado (dedup) o `external_message_id` sin
  match en BD (se ignora silenciosamente, `webhooks_router.py:109-115`).
- Para Resend, configurar `webhook_secret` (`whsec_...`) en las credenciales del
  tenant; sin él la verificación falla (`resend.py:185-188`).

---

## 5. tracking (pixel + click)

**Qué es.** `GET /v1/track/email/open/{message_id}?sig=...` y
`/click/{message_id}?u=...&sig=...` (`app/routers/tracking_router.py:61`,
`104`). Públicos, sin API key, pero con firma HMAC obligatoria (ver ADR-008).

**Comportamiento.**
- open: valida firma, registra evento `opened`, devuelve GIF 1x1
  (`tracking_router.py:61-101`). Firma inválida → 404.
- click: valida URL (`http/https`), valida firma de `(message_id,url)`, aplica
  allowlist de dominios del tenant si existe, registra `clicked`, redirige 302
  (`tracking_router.py:104-176`).

**Diagnóstico.**
- Clicks que dan 404 con firma válida → el dominio destino no está en
  `tenant_tracking_allowlist` del tenant (`tracking_router.py:147-155`). Agregar
  el dominio o desactivar filas para modo permisivo.
- El pixel y el click NO bloquean la respuesta si el INSERT del evento falla
  (`tracking_router.py:97-99`, `172-174`).
- Tras rotar `AES_KEY`, todas las URLs de tracking ya emitidas dan 404 (la
  firma deja de validar). Ver `DEPLOY.md` sobre rotación de secretos.

---

## 6. Reverse proxy (Nginx)

> **[TODO: verificar config Nginx real en VPS]**

Notas conocidas para verificar contra el VPS:
- El contenedor escucha en `8001`; uvicorn corre con `--proxy-headers` y
  `--forwarded-allow-ips 127.0.0.1,172.16.0.0/12,192.168.0.0/16`
  (`Dockerfile:65-70`). Nginx debe enviar `X-Forwarded-For` desde una IP dentro
  de esos rangos para que se respete.
- Headers de seguridad deseados (HSTS, X-Frame-Options DENY, X-Content-Type
  nosniff, CSP `default-src 'none'; frame-ancestors 'none'`) están en el
  `Caddyfile:19-40` de referencia; confirmar que el `server {}` de Nginx los
  replica.
- El healthcheck del proxy debe apuntar a `/health`.
