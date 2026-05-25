-- =============================================================================
-- inovaweb-centro-mensajes — Migracion 002: constraints de seguridad
-- =============================================================================
-- Proposito: forzar invariantes criticas en la BD, no solo en codigo Python.
-- Si el codigo se equivoca o un atacante obtiene acceso SQL directo, los
-- triggers son la red de seguridad que no depende del desarrollador.
--
-- Reglas:
--   1. messages: no DELETE.
--   2. messages: columnas criticas inmutables (id, tenant_id, channel, etc.).
--   3. messages: lifecycle valido de status.
--   4. CHECKs de enums (channel, origin_kind, status, ledger_status, currency).
--   5. templates: inmutables una vez creadas (correccion = nueva version).
--   6. api_keys: no DELETE (solo revocacion via is_active=false + revoked_at).
--   7. tenant_channel_credentials: no DELETE (rotacion via is_active=false).
--   8. message_events: append-only (no UPDATE ni DELETE).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. messages — Append-only: prohibir DELETE
-- -----------------------------------------------------------------------------
-- Un despacho NUNCA debe desaparecer. Para "revertir" un cargo, se agrega
-- una entry inversa en finanzas-core con source_ref de patron
-- msg-<channel>-<message_id>-reversal. El registro original permanece.

CREATE OR REPLACE FUNCTION messages_block_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'DELETE no permitido en messages (append-only). '
        'Para reversar cobro, insertar entry inversa en finanzas-core con '
        'source_ref = "msg-<channel>-<message_id>-reversal".';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_block_delete ON messages;
CREATE TRIGGER trg_messages_block_delete
    BEFORE DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_block_delete();


-- -----------------------------------------------------------------------------
-- 2. messages — Inmutabilidad de columnas criticas
-- -----------------------------------------------------------------------------
-- Estas columnas describen el despacho ORIGINAL. Cambiarlas equivale a
-- falsificar el historial. Las unicas mutaciones permitidas son:
--   status, sent_at, delivered_at, failed_at, events_count, last_error,
--   ledger_* (request_id, entry_id, recorded_at, status, attempts, errors),
--   external_message_id (solo si era NULL — primer asentamiento),
--   updated_at.

CREATE OR REPLACE FUNCTION messages_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id                   IS DISTINCT FROM OLD.id                   THEN
        RAISE EXCEPTION 'messages.id es inmutable';
    END IF;
    IF NEW.tenant_id            IS DISTINCT FROM OLD.tenant_id            THEN
        RAISE EXCEPTION 'messages.tenant_id es inmutable';
    END IF;
    IF NEW.app_id               IS DISTINCT FROM OLD.app_id               THEN
        RAISE EXCEPTION 'messages.app_id es inmutable';
    END IF;
    IF NEW.client_id            IS DISTINCT FROM OLD.client_id            THEN
        RAISE EXCEPTION 'messages.client_id es inmutable';
    END IF;
    IF NEW.service_id           IS DISTINCT FROM OLD.service_id           THEN
        RAISE EXCEPTION 'messages.service_id es inmutable';
    END IF;
    IF NEW.channel              IS DISTINCT FROM OLD.channel              THEN
        RAISE EXCEPTION 'messages.channel es inmutable';
    END IF;
    IF NEW.origin_kind          IS DISTINCT FROM OLD.origin_kind          THEN
        RAISE EXCEPTION 'messages.origin_kind es inmutable';
    END IF;
    IF NEW.template_id          IS DISTINCT FROM OLD.template_id          THEN
        RAISE EXCEPTION 'messages.template_id es inmutable';
    END IF;
    IF NEW.template_slug        IS DISTINCT FROM OLD.template_slug        THEN
        RAISE EXCEPTION 'messages.template_slug es inmutable';
    END IF;
    IF NEW.template_version     IS DISTINCT FROM OLD.template_version     THEN
        RAISE EXCEPTION 'messages.template_version es inmutable';
    END IF;
    IF NEW.from_email           IS DISTINCT FROM OLD.from_email           THEN
        RAISE EXCEPTION 'messages.from_email es inmutable';
    END IF;
    IF NEW.from_phone_id        IS DISTINCT FROM OLD.from_phone_id        THEN
        RAISE EXCEPTION 'messages.from_phone_id es inmutable';
    END IF;
    IF NEW.to_email             IS DISTINCT FROM OLD.to_email             THEN
        RAISE EXCEPTION 'messages.to_email es inmutable';
    END IF;
    IF NEW.to_phone             IS DISTINCT FROM OLD.to_phone             THEN
        RAISE EXCEPTION 'messages.to_phone es inmutable';
    END IF;
    IF NEW.subject              IS DISTINCT FROM OLD.subject              THEN
        RAISE EXCEPTION 'messages.subject es inmutable';
    END IF;
    IF NEW.body_html            IS DISTINCT FROM OLD.body_html            THEN
        RAISE EXCEPTION 'messages.body_html es inmutable';
    END IF;
    IF NEW.body_text            IS DISTINCT FROM OLD.body_text            THEN
        RAISE EXCEPTION 'messages.body_text es inmutable';
    END IF;
    IF NEW.message_text         IS DISTINCT FROM OLD.message_text         THEN
        RAISE EXCEPTION 'messages.message_text es inmutable';
    END IF;
    -- amount_cents_charged: solo permitir transicion NULL -> valor (snapshot del
    -- catalogo de precios al momento del despacho). Una vez seteado, inmutable.
    IF OLD.amount_cents_charged IS NOT NULL
       AND NEW.amount_cents_charged IS DISTINCT FROM OLD.amount_cents_charged THEN
        RAISE EXCEPTION 'messages.amount_cents_charged solo puede setearse una vez';
    END IF;
    IF NEW.currency             IS DISTINCT FROM OLD.currency             THEN
        RAISE EXCEPTION 'messages.currency es inmutable';
    END IF;
    IF NEW.queued_at            IS DISTINCT FROM OLD.queued_at            THEN
        RAISE EXCEPTION 'messages.queued_at es inmutable';
    END IF;
    IF NEW.created_at           IS DISTINCT FROM OLD.created_at           THEN
        RAISE EXCEPTION 'messages.created_at es inmutable';
    END IF;
    -- external_message_id: solo permitir transicion NULL -> valor.
    IF OLD.external_message_id IS NOT NULL
       AND NEW.external_message_id IS DISTINCT FROM OLD.external_message_id THEN
        RAISE EXCEPTION 'messages.external_message_id solo puede setearse una vez';
    END IF;
    -- ledger_request_id: misma regla (NULL -> valor, despues inmutable).
    IF OLD.ledger_request_id IS NOT NULL
       AND NEW.ledger_request_id IS DISTINCT FROM OLD.ledger_request_id THEN
        RAISE EXCEPTION 'messages.ledger_request_id solo puede setearse una vez';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_block_mutation ON messages;
CREATE TRIGGER trg_messages_block_mutation
    BEFORE UPDATE ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_block_mutation();


-- -----------------------------------------------------------------------------
-- 3. messages — Lifecycle valido de status
-- -----------------------------------------------------------------------------
-- queued    -> sent | failed
-- sent      -> delivered | bounced | failed
-- delivered -> (terminal, salvo complaint o reaperturas que NO cambian status)
-- failed    -> (terminal)
-- bounced   -> (terminal)

CREATE OR REPLACE FUNCTION messages_validate_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'queued'    AND NEW.status IN ('sent', 'failed')                 THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'sent'      AND NEW.status IN ('delivered', 'bounced', 'failed') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'Transicion invalida de status en messages: % -> %',
        OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_status_lifecycle ON messages;
CREATE TRIGGER trg_messages_status_lifecycle
    BEFORE UPDATE OF status ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_validate_status_transition();


-- -----------------------------------------------------------------------------
-- 4. CHECKs de enums y formatos
-- -----------------------------------------------------------------------------
-- Usar CHECK en lugar de ENUM nativo para agregar valores sin downtime.

-- messages.channel
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_channel;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_channel
    CHECK (channel IN ('email', 'whatsapp', 'sms'));

-- messages.origin_kind: obligatorio si channel='email', NULL en otros.
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_origin_kind;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_origin_kind
    CHECK (
        (channel = 'email' AND origin_kind IN ('template', 'ai_generated'))
        OR
        (channel <> 'email' AND origin_kind IS NULL)
    );

-- messages.status
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_status;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_status
    CHECK (status IN ('queued', 'sent', 'delivered', 'failed', 'bounced'));

-- messages.ledger_status
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_ledger_status;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_ledger_status
    CHECK (ledger_status IN ('not_applicable', 'pending', 'recorded', 'failed', 'manual'));

-- messages.currency (ISO 4217: 3 chars en mayusculas).
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_currency_iso;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_currency_iso
    CHECK (currency ~ '^[A-Z]{3}$');

-- messages.amount_cents_charged: si esta seteado, debe ser > 0.
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_amount_positive;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_amount_positive
    CHECK (amount_cents_charged IS NULL OR amount_cents_charged > 0);

-- messages: destinatario coherente con el canal.
-- email exige to_email; whatsapp/sms exigen to_phone.
ALTER TABLE messages
    DROP CONSTRAINT IF EXISTS chk_messages_destination;
ALTER TABLE messages
    ADD  CONSTRAINT chk_messages_destination
    CHECK (
        (channel = 'email'                AND to_email IS NOT NULL)
        OR
        (channel IN ('whatsapp', 'sms')   AND to_phone IS NOT NULL)
    );

-- templates.channel
ALTER TABLE templates
    DROP CONSTRAINT IF EXISTS chk_templates_channel;
ALTER TABLE templates
    ADD  CONSTRAINT chk_templates_channel
    CHECK (channel IN ('email', 'whatsapp', 'sms'));

-- tenant_channel_credentials.channel
ALTER TABLE tenant_channel_credentials
    DROP CONSTRAINT IF EXISTS chk_tcc_channel;
ALTER TABLE tenant_channel_credentials
    ADD  CONSTRAINT chk_tcc_channel
    CHECK (channel IN ('email', 'whatsapp', 'sms'));

-- message_providers.channel
ALTER TABLE message_providers
    DROP CONSTRAINT IF EXISTS chk_message_providers_channel;
ALTER TABLE message_providers
    ADD  CONSTRAINT chk_message_providers_channel
    CHECK (channel IN ('email', 'whatsapp', 'sms'));


-- -----------------------------------------------------------------------------
-- 5. templates — inmutables una vez creadas
-- -----------------------------------------------------------------------------
-- Una plantilla referenciada por un mensaje historico debe seguir siendo
-- legible exactamente como fue. Correccion = nueva fila con version+=1.
-- Solo permitimos togglear is_active (para "retirar" una plantilla de uso
-- futuro sin afectar la auditoria de mensajes pasados).

CREATE OR REPLACE FUNCTION templates_block_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'DELETE no permitido en templates. Las plantillas son inmutables. '
        'Para retirar de uso, set is_active=false.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_templates_block_delete ON templates;
CREATE TRIGGER trg_templates_block_delete
    BEFORE DELETE ON templates
    FOR EACH ROW EXECUTE FUNCTION templates_block_delete();

CREATE OR REPLACE FUNCTION templates_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id                 IS DISTINCT FROM OLD.id                 THEN
        RAISE EXCEPTION 'templates.id es inmutable'; END IF;
    IF NEW.tenant_id          IS DISTINCT FROM OLD.tenant_id          THEN
        RAISE EXCEPTION 'templates.tenant_id es inmutable'; END IF;
    IF NEW.slug               IS DISTINCT FROM OLD.slug               THEN
        RAISE EXCEPTION 'templates.slug es inmutable'; END IF;
    IF NEW.version            IS DISTINCT FROM OLD.version            THEN
        RAISE EXCEPTION 'templates.version es inmutable'; END IF;
    IF NEW.channel            IS DISTINCT FROM OLD.channel            THEN
        RAISE EXCEPTION 'templates.channel es inmutable'; END IF;
    IF NEW.subject_template   IS DISTINCT FROM OLD.subject_template   THEN
        RAISE EXCEPTION 'templates.subject_template es inmutable'; END IF;
    IF NEW.body_html_template IS DISTINCT FROM OLD.body_html_template THEN
        RAISE EXCEPTION 'templates.body_html_template es inmutable'; END IF;
    IF NEW.body_text_template IS DISTINCT FROM OLD.body_text_template THEN
        RAISE EXCEPTION 'templates.body_text_template es inmutable'; END IF;
    IF NEW.message_template   IS DISTINCT FROM OLD.message_template   THEN
        RAISE EXCEPTION 'templates.message_template es inmutable'; END IF;
    IF NEW.variables_schema   IS DISTINCT FROM OLD.variables_schema   THEN
        RAISE EXCEPTION 'templates.variables_schema es inmutable'; END IF;
    IF NEW.created_at         IS DISTINCT FROM OLD.created_at         THEN
        RAISE EXCEPTION 'templates.created_at es inmutable'; END IF;
    -- Permite togglear is_active, name, metadata.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_templates_block_mutation ON templates;
CREATE TRIGGER trg_templates_block_mutation
    BEFORE UPDATE ON templates
    FOR EACH ROW EXECUTE FUNCTION templates_block_mutation();


-- -----------------------------------------------------------------------------
-- 6. api_keys — no DELETE (solo revocacion)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION api_keys_block_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'DELETE no permitido en api_keys. Para invalidar, set is_active=false y revoked_at=now().';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_api_keys_block_delete ON api_keys;
CREATE TRIGGER trg_api_keys_block_delete
    BEFORE DELETE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION api_keys_block_delete();


-- -----------------------------------------------------------------------------
-- 7. tenant_channel_credentials — no DELETE (rotacion via is_active=false)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION tcc_block_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'DELETE no permitido en tenant_channel_credentials. '
        'Para rotar, agregar nueva fila y set is_active=false en la antigua.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tcc_block_delete ON tenant_channel_credentials;
CREATE TRIGGER trg_tcc_block_delete
    BEFORE DELETE ON tenant_channel_credentials
    FOR EACH ROW EXECUTE FUNCTION tcc_block_delete();


-- -----------------------------------------------------------------------------
-- 8. message_events — append-only (no UPDATE, no DELETE)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION message_events_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'UPDATE/DELETE no permitidos en message_events (append-only). '
        'Eventos webhook son inmutables una vez recibidos.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_message_events_block_update ON message_events;
CREATE TRIGGER trg_message_events_block_update
    BEFORE UPDATE ON message_events
    FOR EACH ROW EXECUTE FUNCTION message_events_block_mutation();

DROP TRIGGER IF EXISTS trg_message_events_block_delete ON message_events;
CREATE TRIGGER trg_message_events_block_delete
    BEFORE DELETE ON message_events
    FOR EACH ROW EXECUTE FUNCTION message_events_block_mutation();


-- -----------------------------------------------------------------------------
-- VERIFICACION
-- -----------------------------------------------------------------------------
SELECT
    trigger_name,
    event_manipulation,
    event_object_table
FROM information_schema.triggers
WHERE event_object_table IN (
    'messages', 'message_events', 'templates',
    'api_keys', 'tenant_channel_credentials'
)
ORDER BY event_object_table, trigger_name;
