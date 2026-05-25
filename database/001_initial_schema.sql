-- =============================================================================
-- inovaweb-centro-mensajes — esquema inicial
-- =============================================================================
-- Migracion 001: esquema canonico del Centro de Mensajes como core Nivel 1.
--
-- Tablas:
--   1. tenants                       — multi-tenant root
--   2. api_keys                      — auth X-API-Key + scopes (hash SHA-256)
--   3. message_providers             — catalogo de proveedores externos
--   4. templates                     — plantillas versionadas e inmutables
--   5. tenant_channel_credentials    — credenciales por tenant+canal (AES-256-GCM)
--   6. messages                      — log canonico de despachos + trazabilidad ledger
--   7. message_events                — eventos webhook (delivered, opened, clicked)
--
-- Convenciones:
--   - Toda moneda en centavos enteros BIGINT. NUNCA floats.
--   - SHA-256 para hash de API keys, plaintext jamas en BD.
--   - AES-256-GCM para credenciales de proveedores en tenant_channel_credentials.
--   - Multi-tenant strict: toda query filtra por tenant_id resuelto de la API key.
--   - Append-only en messages, templates, api_keys (enforced en migracion 002).
--   - Canales cerrados: email | whatsapp | sms.
--   - origin_kind obligatorio para email: template | ai_generated.
--   - source_ref al ledger: msg-<channel>-<message_id> (idempotente).
--
-- Postgres ejecuta este archivo UNA SOLA VEZ en el primer arranque del
-- contenedor (mecanismo /docker-entrypoint-initdb.d). Idempotente con
-- IF NOT EXISTS + ON CONFLICT DO NOTHING.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Extensiones requeridas
-- -----------------------------------------------------------------------------
-- gen_random_uuid() vive en pgcrypto en Postgres < 13, builtin desde 13.
-- Postgres 16 ya lo trae nativo, pero CREATE EXTENSION es no-op si existe.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- -----------------------------------------------------------------------------
-- 1. TENANTS
--    Clientes del centro. Mantener UUID compatible con tenants del Medidor IA,
--    Hub de Pasarelas y Finanzas-Core para correlacion cross-servicio.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         VARCHAR(80)  NOT NULL UNIQUE,
    name         VARCHAR(200) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    metadata     JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenants_active ON tenants (is_active) WHERE is_active = true;


-- -----------------------------------------------------------------------------
-- 2. API_KEYS
--    Autenticacion del centro via header X-API-Key. Plaintext jamas en BD.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                  VARCHAR(120) NOT NULL,
    key_prefix            VARCHAR(20)  NOT NULL UNIQUE,
    key_hash              VARCHAR(255) NOT NULL UNIQUE,
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Scopes posibles:
    --   messages:write           — POST /v1/messages/{channel}
    --   messages:read            — GET /v1/messages propios del tenant
    --   messages:read:financial  — admin-financiera (cross-tenant en filtros)
    --   admin:templates          — CRUD plantillas
    --   admin:credentials        — CRUD credenciales de proveedor
    --   *                        — wildcard admin master
    scopes                TEXT[] NOT NULL DEFAULT '{}',
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    is_active             BOOLEAN NOT NULL DEFAULT true,
    last_used_at          TIMESTAMPTZ,
    expires_at            TIMESTAMPTZ,
    revoked_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash    ON api_keys (key_hash)   WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix  ON api_keys (key_prefix);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant  ON api_keys (tenant_id);


-- -----------------------------------------------------------------------------
-- 3. MESSAGE_PROVIDERS
--    Catalogo de proveedores externos soportados. Cerrado en codigo.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_providers (
    slug            VARCHAR(50) PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    -- Canal que atiende este proveedor.
    channel         VARCHAR(20)  NOT NULL,
    -- 'active' (productivo) | 'stub' (interfaz lista, sin implementacion real)
    status          VARCHAR(20)  NOT NULL DEFAULT 'stub',
    description     TEXT,
    config_schema   JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO message_providers (slug, name, channel, status, description, config_schema)
VALUES
    ('resend',        'Resend',                'email',    'active',
     'Email transaccional. API key + dominio verificado. Webhooks firmados con svix.',
     '{"api_key":"string","webhook_secret":"string","default_from":"string"}'),
    ('sendgrid',      'SendGrid',              'email',    'stub',
     'Email alternativo. API key + dominio verificado.',
     '{"api_key":"string","webhook_verification_key":"string"}'),
    ('meta_whatsapp', 'Meta Cloud API',        'whatsapp', 'stub',
     'WhatsApp Business via graph.facebook.com. Access token + phone_number_id.',
     '{"access_token":"string","phone_number_id":"string","app_secret":"string"}'),
    ('twilio',        'Twilio',                'sms',      'stub',
     'SMS (y WhatsApp alternativo). Account SID + Auth token + Messaging Service SID.',
     '{"account_sid":"string","auth_token":"string","messaging_service_sid":"string"}')
ON CONFLICT (slug) DO NOTHING;


-- -----------------------------------------------------------------------------
-- 4. TEMPLATES
--    Plantillas pre-aprobadas con variables tipadas. INMUTABLES una vez creadas.
--    Para corregir, se crea una nueva version (mismo slug, version += 1).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS templates (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Slug logico de la plantilla (ej: 'tpl-boleta-mensual').
    slug                  VARCHAR(120) NOT NULL,
    -- Numero de version. La combinacion (tenant, slug, version) es unica.
    version               INTEGER NOT NULL CHECK (version > 0),
    -- Canal que esta plantilla atiende.
    channel               VARCHAR(20)  NOT NULL,
    name                  VARCHAR(200) NOT NULL,
    -- Para email
    subject_template      TEXT,
    body_html_template    TEXT,
    body_text_template    TEXT,
    -- Para SMS / WhatsApp con cuerpo simple
    message_template      TEXT,
    -- Schema declarado de variables esperadas. Validado al alta y al despachar.
    -- Ejemplo: {"alumno":"string","periodo":"string","materias":"array"}
    variables_schema      JSONB NOT NULL DEFAULT '{}',
    -- Metadatos libres (proveedor recomendado, language, etc.)
    metadata              JSONB NOT NULL DEFAULT '{}',
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_by_api_key_id UUID REFERENCES api_keys(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, slug, version)
);

CREATE INDEX IF NOT EXISTS idx_templates_tenant_slug
    ON templates (tenant_id, slug, version DESC);
CREATE INDEX IF NOT EXISTS idx_templates_active
    ON templates (tenant_id, channel)
    WHERE is_active = true;


-- -----------------------------------------------------------------------------
-- 5. TENANT_CHANNEL_CREDENTIALS
--    Credenciales del proveedor externo por tenant y canal. Cifradas con
--    AES-256-GCM antes de tocar disco. JAMAS exponer en endpoints de lectura.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_channel_credentials (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel          VARCHAR(20) NOT NULL,
    provider_slug    VARCHAR(50) NOT NULL REFERENCES message_providers(slug) ON DELETE RESTRICT,
    -- JSON cifrado con AES-256-GCM (nonce + ciphertext + tag, base64).
    encrypted_value  TEXT NOT NULL,
    is_default       BOOLEAN NOT NULL DEFAULT false,
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, provider_slug)
);

-- Solo una credencial default activa por tenant+canal.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_channel_default
    ON tenant_channel_credentials (tenant_id, channel)
    WHERE is_default = true AND is_active = true;


-- -----------------------------------------------------------------------------
-- 6. MESSAGES
--    Log canonico de cada despacho. APPEND-ONLY (enforced en migracion 002).
--    Incluye trazabilidad al Finanzas-Core (cierre del loop despacho -> cobro).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Identificacion del origen (segun caller).
    app_id                VARCHAR(80),
    client_id             VARCHAR(120),
    service_id            VARCHAR(120),

    -- Canal y vía de origen.
    channel               VARCHAR(20) NOT NULL,
    -- Obligatorio para email: 'template' | 'ai_generated'. NULL para otros canales.
    origin_kind           VARCHAR(20),

    -- Plantilla referenciada (cuando origin_kind='template').
    template_id           UUID REFERENCES templates(id) ON DELETE SET NULL,
    template_slug         VARCHAR(120),
    template_version      INTEGER,

    -- Remitente. Email lleva from_email+from_name. WA/SMS lleva from_phone_id.
    from_email            VARCHAR(320),
    from_name             VARCHAR(200),
    from_phone_id         VARCHAR(30),

    -- Destinatario (al menos uno segun canal).
    to_email              VARCHAR(320),
    to_name               VARCHAR(200),
    to_phone              VARCHAR(30),

    -- Contenido renderizado (snapshot al momento del despacho).
    subject               VARCHAR(500),
    body_html             TEXT,
    body_text             TEXT,
    message_text          TEXT,             -- SMS / WhatsApp simple

    -- Variables hidratadas (cuando origin_kind='template').
    variables             JSONB,

    -- Tracking opcional para email.
    tracking_open         BOOLEAN NOT NULL DEFAULT false,
    tracking_click        BOOLEAN NOT NULL DEFAULT false,

    -- Metadatos libres del caller (medidor_event_id, model, tokens, etc.).
    meta                  JSONB NOT NULL DEFAULT '{}',

    -- Proveedor que despacho este mensaje.
    provider_slug         VARCHAR(50),

    -- Lifecycle del mensaje.
    -- queued -> sent -> delivered | bounced | failed
    status                VARCHAR(20) NOT NULL DEFAULT 'queued',

    -- Identificador externo del proveedor (re_xxx en Resend, wamid en Meta, etc.)
    external_message_id   VARCHAR(200) UNIQUE,

    -- Timestamps del lifecycle.
    queued_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at               TIMESTAMPTZ,
    delivered_at          TIMESTAMPTZ,
    failed_at             TIMESTAMPTZ,

    -- Contador de eventos asociados (incrementable por trigger).
    events_count          INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,

    -- Monto cobrado en el ledger por este mensaje (snapshot del catalogo).
    amount_cents_charged  BIGINT,
    currency              CHAR(3) NOT NULL DEFAULT 'MXN',

    -- ── Trazabilidad al Finanzas-Core (downstream debit) ─────────────────────
    -- source_ref enviado al ledger (msg-<channel>-<message_id>).
    ledger_request_id     VARCHAR(120) UNIQUE,
    ledger_entry_id       UUID,
    ledger_recorded_at    TIMESTAMPTZ,
    -- not_applicable | pending | recorded | failed | manual
    ledger_status         VARCHAR(20) NOT NULL DEFAULT 'not_applicable',
    ledger_attempts       INTEGER NOT NULL DEFAULT 0,
    ledger_last_attempt_at TIMESTAMPTZ,
    ledger_last_error     TEXT,

    -- Quien envio (trazabilidad de actor).
    actor_api_key_id      UUID REFERENCES api_keys(id) ON DELETE SET NULL,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indices de consulta multi-eje (filtros tipicos del dueño del proceso).
CREATE INDEX IF NOT EXISTS idx_messages_tenant_created
    ON messages (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_tenant_app
    ON messages (tenant_id, app_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_tenant_client
    ON messages (tenant_id, client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_tenant_channel_status
    ON messages (tenant_id, channel, status);
CREATE INDEX IF NOT EXISTS idx_messages_external_id
    ON messages (external_message_id)
    WHERE external_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_template
    ON messages (tenant_id, template_slug, template_version)
    WHERE template_id IS NOT NULL;

-- Indice critico para el job de reintento al finanzas-core.
CREATE INDEX IF NOT EXISTS idx_messages_ledger_retry
    ON messages (ledger_status, ledger_last_attempt_at)
    WHERE ledger_status IN ('pending', 'failed');


-- -----------------------------------------------------------------------------
-- 7. MESSAGE_EVENTS
--    Eventos asincronos recibidos por webhook (delivered, bounced, opened,
--    clicked, dropped). Deduplicado por (external_message_id, event_type).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_events (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id            UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- sent | delivered | bounced | dropped | opened | clicked | failed | complaint
    event_type            VARCHAR(40) NOT NULL,
    external_message_id   VARCHAR(200),
    provider_slug         VARCHAR(50),
    -- URL clickeada (para event_type='clicked').
    url_clicked           TEXT,
    -- IP y user-agent (para opened/clicked).
    ip_address            INET,
    user_agent            VARCHAR(500),
    -- Payload crudo del proveedor (depurado de PII sensible). Util para debug.
    raw_payload           JSONB,
    occurred_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Dedup: el mismo external_id + event_type no se procesa dos veces.
    UNIQUE (external_message_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_message_events_message
    ON message_events (message_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_message_events_tenant_type
    ON message_events (tenant_id, event_type, occurred_at DESC);


-- -----------------------------------------------------------------------------
-- TRIGGERS — updated_at automatico
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION messages_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenants',
        'message_providers',
        'tenant_channel_credentials',
        'messages'
    ] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_updated_at ON %s;
             CREATE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON %s
             FOR EACH ROW EXECUTE FUNCTION messages_set_updated_at()',
            t, t, t, t
        );
    END LOOP;
END;
$$;


-- -----------------------------------------------------------------------------
-- TRIGGER — incrementar events_count cuando llega un message_event
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION messages_bump_events_count()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE messages
       SET events_count = events_count + 1
     WHERE id = NEW.message_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_message_events_bump ON message_events;
CREATE TRIGGER trg_message_events_bump
    AFTER INSERT ON message_events
    FOR EACH ROW EXECUTE FUNCTION messages_bump_events_count();


-- -----------------------------------------------------------------------------
-- COMMENTS
-- -----------------------------------------------------------------------------
COMMENT ON TABLE  messages IS
    'Log canonico de despachos. Append-only para auditoria (enforced por triggers '
    'en migracion 002). Las columnas ledger_* cierran el loop despacho -> cobro '
    'en Finanzas-Core con source_ref idempotente msg-<channel>-<message_id>.';

COMMENT ON COLUMN messages.origin_kind IS
    'Solo email: ''template'' (hidrata plantilla con variables) o ''ai_generated'' '
    '(cuerpo construido aguas arriba via Medidor IA). NULL para WhatsApp/SMS.';

COMMENT ON COLUMN messages.ledger_request_id IS
    'Idempotency key del POST al Finanzas-Core. Convencion: msg-<channel>-<message_id>. '
    'UNIQUE garantiza que el reintento no duplica el cargo en el ledger.';

COMMENT ON COLUMN messages.ledger_status IS
    'Estado del POST al ledger: not_applicable (mensaje sin cobro), '
    'pending (en cola), recorded (exitoso), failed (en reintento), manual (excedio reintentos).';

COMMENT ON TABLE  templates IS
    'Catalogo de plantillas pre-aprobadas. INMUTABLES una vez creadas. '
    'Correcciones generan nueva version (mismo slug, version += 1).';


-- -----------------------------------------------------------------------------
-- VERIFICACION FINAL
-- -----------------------------------------------------------------------------
SELECT
    table_name,
    (SELECT COUNT(*) FROM information_schema.columns c
      WHERE c.table_name = t.table_name AND c.table_schema = 'public') AS columnas
FROM information_schema.tables t
WHERE t.table_schema = 'public'
  AND t.table_name IN (
    'tenants',
    'api_keys',
    'message_providers',
    'templates',
    'tenant_channel_credentials',
    'messages',
    'message_events'
  )
ORDER BY t.table_name;
