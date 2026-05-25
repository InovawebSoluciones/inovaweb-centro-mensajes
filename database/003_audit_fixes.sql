-- =============================================================================
-- inovaweb-centro-mensajes — Migracion 003: fixes de auditoria 4-ojos
-- =============================================================================
-- Esta migracion arregla problemas detectados por la auditoria pre-deploy:
--   1. Trigger messages_block_mutation rechazaba transicion legitima
--      NULL -> valor en amount_cents_charged (rompia el flujo de email).
--   2. Agregar columna messages.dispatch_attempts y messages.last_dispatch_error
--      para soportar reintentos del dispatch a proveedor (no solo del ledger).
--   3. Indice adicional para que el worker ledger_retry capture
--      ledger_status='pending' que quedan huerfanos por crash del proceso.
--
-- Esta migracion ES IDEMPOTENTE y SE PUEDE APLICAR sobre BD existente.
-- Para deploys nuevos, los cambios ya estan en 001/002 (consolidados).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Reemplazar trigger messages_block_mutation para permitir NULL -> valor
--    en amount_cents_charged (snapshot al despachar). Sigue siendo inmutable
--    una vez seteado.
-- -----------------------------------------------------------------------------
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
    -- amount_cents_charged: NULL -> valor permitido (snapshot al despachar).
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


-- -----------------------------------------------------------------------------
-- 2. Agregar columnas para reintento del dispatch a proveedor.
--    El worker (a implementar) tomara messages en status='queued' o 'failed'
--    transitorio para reintentar.
-- -----------------------------------------------------------------------------
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS dispatch_attempts   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_dispatch_error TEXT;


-- -----------------------------------------------------------------------------
-- 3. Indice para que ledger_retry capture pending huerfanos (proceso muerto
--    entre marcar pending y persistir resultado del POST al finanzas-core).
-- -----------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_messages_ledger_retry;
CREATE INDEX IF NOT EXISTS idx_messages_ledger_retry
    ON messages (ledger_status, ledger_last_attempt_at)
    WHERE ledger_status IN ('pending', 'failed');


-- -----------------------------------------------------------------------------
-- 4. Tabla opcional de allowlist de dominios para click tracking por tenant.
--    Si esta vacia para un tenant, NO se aplica restriccion (modo permisivo
--    con firma HMAC obligatoria, ver app/core/tracking_signing.py).
--    Si tiene rows, el endpoint /v1/track/email/click rechaza redirects fuera
--    de los dominios listados.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_tracking_allowlist (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    domain       VARCHAR(253) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_tracking_allowlist_tenant
    ON tenant_tracking_allowlist (tenant_id)
    WHERE is_active = true;


-- -----------------------------------------------------------------------------
-- 5. Verificacion
-- -----------------------------------------------------------------------------
SELECT 'OK 003 applied' AS status;
