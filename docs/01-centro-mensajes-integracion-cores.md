# Centro de Mensajes - Contrato de integracion con otros cores

**Estado:** especificacion para sprints iniciales del proyecto. Define
como las apps cliente invocan al Centro de Mensajes y como el Centro
reporta cada despacho al Finanzas-Core.

---

## 1. Vision rapida

```
[app cliente Nivel 3] ── POST /v1/messages/{channel} ──► [centro-mensajes]
                                                              │
                                                              ├─ valida key, scope, plantilla
                                                              ├─ resuelve credenciales del proveedor
                                                              ├─ despacha via httpx
                                                              ├─ persiste registro append-only
                                                              │
                                                              └─ POST /v1/ledger/entries ──► [finanzas-core]
                                                                  source=messages
                                                                  direction=debit
                                                                  source_ref=msg-<channel>-<message_id>
```

---

## 2. Endpoints de despacho

Tres endpoints simetricos por canal:

- `POST https://mensajes.inovaweb.com.mx/v1/messages/email`
- `POST https://mensajes.inovaweb.com.mx/v1/messages/whatsapp`
- `POST https://mensajes.inovaweb.com.mx/v1/messages/sms`

**Headers comunes:**
- `X-API-Key: <key del consumidor>` (scope `messages:write`)
- `Content-Type: application/json`

**Respuesta exitosa (202 Accepted):**
```json
{
  "message_id": "uuid-server-side",
  "tenant_id": "uuid-resuelto-de-la-key",
  "channel": "email | whatsapp | sms",
  "status": "queued",
  "queued_at": "2026-06-01T15:00:00Z"
}
```

El despacho es asincrono. El estado real (`sent`, `delivered`, `failed`,
`bounced`) se conoce despues via webhook del proveedor y se consulta con
`GET /v1/messages/{message_id}`.

---

## 3. Esquemas por canal

### 3.1 Email con plantilla pre-registrada

```json
{
  "app_id": "webescolar",
  "client_id": "escuela-123",
  "service_id": "boleta-mensual",
  "origin_kind": "template",
  "template_id": "tpl-boleta-mensual-v3",
  "from": { "email": "noreply@escuela123.inovaweb.com.mx", "name": "Escuela 123" },
  "to": { "email": "padre@ejemplo.com", "name": "Maria Lopez" },
  "variables": {
    "alumno": "Juan Lopez",
    "periodo": "Mayo 2026",
    "materias": [{ "nombre": "Matematicas", "calificacion": 9.2 }]
  },
  "tracking": { "open": true, "click": true }
}
```

### 3.2 Email con cuerpo generado por IA

```json
{
  "app_id": "scraping",
  "client_id": "client-uuid-001",
  "service_id": "envio-frio",
  "origin_kind": "ai_generated",
  "from": { "email": "envios@inovaweb.com.mx", "name": "Inovaweb" },
  "to": { "email": "destino@ejemplo.com", "name": "Juan Perez" },
  "subject": "Propuesta personalizada",
  "body_html": "<p>Hola Juan, ...</p>",
  "body_text": "Hola Juan, ...",
  "tracking": { "open": true, "click": true },
  "meta": {
    "medidor_event_id": "evt_xyz789",
    "model": "deepseek-chat",
    "tokens_in": 2629,
    "tokens_out": 1197
  }
}
```

### 3.3 WhatsApp con plantilla

```json
{
  "app_id": "webescolar",
  "client_id": "escuela-123",
  "service_id": "recordatorio-pago",
  "template_id": "tpl-recordatorio-pago-v2",
  "from_phone_id": "+5215555000001",
  "to_phone": "+5215512345678",
  "variables": {
    "alumno": "Juan Lopez",
    "monto": "1500.00",
    "fecha_limite": "2026-06-05"
  }
}
```

### 3.4 SMS

```json
{
  "app_id": "webescolar",
  "client_id": "escuela-123",
  "service_id": "alerta-puerta",
  "from_phone_id": "+5215555000001",
  "to_phone": "+5215512345678",
  "message": "Su hijo Juan registro entrada a las 07:42"
}
```

---

## 4. Reglas firmes

1. **`tenant_id` NO va en el body.** Se resuelve siempre desde la API key.
2. **Telefonos en E.164** con prefijo de pais (`+52...`), validados al ingreso.
3. **`origin_kind` obligatorio en email**, debe ser `template` o `ai_generated`.
4. **Plantillas inmutables.** Para corregir, crear nueva version via `PATCH
   /admin/v1/templates/{id}` que incrementa el numero de version.
5. **Variables de plantilla validadas** contra el schema declarado al alta.
   Variables faltantes o de tipo incorrecto rechazan con 422.
6. **Despacho asincrono.** 202 al cliente no garantiza entrega final, solo
   aceptacion en cola.
7. **`message_id` es UUID server-side**, no del cliente.

---

## 5. Reporte automatico al Finanzas-Core

El Centro emite, por cada mensaje exitosamente despachado, un POST al
ledger consolidado con el siguiente payload:

```json
{
  "source_slug": "messages",
  "source_ref": "msg-<channel>-<message_id>",
  "direction": "debit",
  "amount_cents": <precio_del_canal>,
  "currency": "MXN",
  "occurred_at": "<timestamp_del_despacho>",
  "description": "<channel> enviado a <destinatario_abreviado> via <template_id | ai_generated>",
  "meta": {
    "app_id": "<app>",
    "client_id": "<client>",
    "service_id": "<servicio>",
    "template_id": "<si aplica>",
    "origin_kind": "<template | ai_generated>",
    "external_message_id": "<id del proveedor>"
  }
}
```

**Endpoint:** `POST https://finanzas.inovaweb.com.mx/v1/ledger/entries`
**Header:** `X-API-Key: <key del centro al ledger>` (scope `ledger:write`,
etiqueta `core-messages`).

**Idempotencia garantizada** por el patron determinista de `source_ref`.

**Politica ante fallo del finanzas-core:** el cargo queda en estado
pendiente local, un job reintenta cada 60 segundos hasta 8 veces, despues
escala a estado `manual` para revision humana. El registro del mensaje NO
se afecta; solo la columna `ledger_status` cambia.

---

## 6. Convenciones de identificadores

| Campo | Patron sugerido | Ejemplo |
|---|---|---|
| `app_id` | slug corto de la app | `webescolar`, `scraping`, `microfichas`, `ecofile` |
| `client_id` | UUID del cliente final del tenant | `client-uuid-001` |
| `service_id` | slug del concepto de mensaje | `boleta-mensual`, `recordatorio-pago`, `envio-frio` |
| `template_id` | slug-version | `tpl-boleta-mensual-v3` |
| `message_id` | UUID server-side | autogenerado por el Centro |
| `source_ref` al ledger | `msg-<channel>-<message_id>` | `msg-email-018e2c7b-a1d4-7c2e-9f3a-1234567890ab` |
| Reversal contable | `<source_ref>-reversal` | `msg-email-018e2c7b-...-reversal` |

---

## 7. Manejo de errores

| Codigo | Causa | Que debe hacer el consumidor |
|---|---|---|
| 202 | Mensaje aceptado para despacho | OK, guardar `message_id` para correlacion |
| 400 | Body invalido (campo faltante, formato malo) | Bug en consumidor, revisar payload |
| 401 | API key invalida, inactiva o expirada | Verificar `.env`, no reintentar |
| 403 | API key no tiene scope `messages:write` | Pedir admin que actualice scopes |
| 404 | `template_id` no existe o no pertenece al tenant | Verificar slug y tenant |
| 422 | Validacion pydantic fallida (telefono mal formado, variable faltante) | Bug en consumidor, NO reintentar |
| 429 | Rate limit excedido | Esperar `Retry-After` y reintentar |
| 500 | Error inesperado del servidor | Reintentar con backoff exponencial |
| 502/503 | Proveedor externo o BD caidos | Reintentar con backoff |

---

## 8. Consulta y reportes

### Para el dueño del proceso (app cliente)

- `GET /v1/messages/{message_id}` - detalle del propio mensaje con eventos.
- `GET /v1/messages?app=...&service=...&from_ts=...&to_ts=...&limit=...` - listado paginado.

### Para Administracion Financiera (Nivel 2, scope `messages:read:financial`)

- `GET /v1/messages?app=...&client=...&channel=...&from_ts=...&to_ts=...` - listado cross-tenant en sus filtros.
- `GET /v1/reports/usage?from_ts=...&to_ts=...&group_by=client,channel` - agregados.

Todas las consultas filtran por `tenant_id` resuelto de la API key, garantizando aislamiento multi-tenant.

---

## 9. Diferencia explicita entre las dos vias de correo

| Concepto | `origin_kind=template` | `origin_kind=ai_generated` |
|---|---|---|
| Quien construye el cuerpo | El Centro hidrata variables en la plantilla | La app cliente entrega el cuerpo ya construido |
| Campos obligatorios | `template_id`, `variables` | `subject`, `body_html` o `body_text`, `meta.medidor_event_id` |
| Precio cobrado al cliente | Tarifa plana del canal | Tarifa del canal + (opcional) componente por origen IA |
| Auditoria | Plantilla version + variables | Cuerpo completo + correlacion con evento del Medidor |
| Uso tipico | Boletas, recordatorios, confirmaciones | Correos frios personalizados, respuestas a clientes |

---

## 10. Roadmap de integracion (sprints siguientes)

| Sprint | Que se hace |
|---|---|
| 1 (actual) | Scaffolding, esquema BD, endpoints de despacho email, integracion Resend, reporte al finanzas-core |
| 2 | Integracion WhatsApp (Meta Cloud API) y SMS (Twilio), webhooks de los 3 proveedores |
| 3 | Sistema de plantillas con variables tipadas, endpoints admin |
| 4 | Tracking pixel y reescritura de links para email, eventos de apertura y clic |
| 5 | Integracion productiva: WebEscolar manda boletas, Scraping manda correos IA |
| 6 | Admin Financiera consume `/v1/reports/usage` para tableros y facturas |
| 7 (futuro) | Canales adicionales: Messenger, Instagram, Push notifications |

---

## 11. Referencias

- Codigo previsto: `app/routers/messages_router.py`, `app/providers/*.py`
- Esquema SQL: `database/001_initial_schema.sql` (tablas tenants, api_keys, templates, tenant_channel_credentials, messages, message_events)
- Triggers append-only: `database/002_security_constraints.sql`
- Cliente al ledger: `app/core/ledger_client.py`
- Documento tecnico completo: `docs/inovaweb-centro-mensajes-proyecto-tecnico.md`
- Contrato del finanzas-core: `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-finanzas-core\docs\01-finanzas-core-integracion-cores.md`
