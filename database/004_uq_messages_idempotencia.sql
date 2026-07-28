-- 004_uq_messages_idempotencia.sql
-- [FEA 2026-07-27] Respalda la idempotencia ATOMICA de mensajes.
-- record_message hace INSERT ... ON CONFLICT (tenant_id, (meta->>'source_ref'))
-- DO NOTHING; ese ON CONFLICT necesita este indice unico parcial por expresion.
-- Antes la idempotencia era check-then-insert (SELECT y luego INSERT, no atomico):
-- un reintento concurrente con el mismo source_ref (caso de uso explicito del
-- endpoint ante timeout) insertaba dos filas 'sent' -> en modo D2 (tarificacion
-- desde la tabla messages) el cliente se cobraba DOS veces el mismo correo.
-- Aplicado en vivo en centro_mensajes el 2026-07-27 (0 duplicados previos).

CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_tenant_sourceref
    ON messages (tenant_id, (meta->>'source_ref'))
    WHERE meta ? 'source_ref';
