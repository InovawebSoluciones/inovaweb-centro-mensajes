"""
workers/ledger_retry.py
=======================
Job de reintento de cargos fallidos al Finanzas-Core.

Busca mensajes con ledger_status='failed', los reintenta con el mismo
source_ref (msg-<channel>-<message_id>) que el primer intento. La
idempotencia esta garantizada por el finanzas-core: reintentar la misma
source_ref devuelve idempotent_replay=true sin duplicar el cargo.

Tras MAX_ATTEMPTS sin exito, promueve a ledger_status='manual' para
revision humana.

Disparo:
  - Loop interno arrancado por lifespan de FastAPI en main.py.
  - Cada vuelta corre un batch (BATCH_SIZE) y duerme LOOP_INTERVAL_SECONDS.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.ledger_client import (
    LedgerAuthError,
    LedgerError,
    LedgerTransientError,
    LedgerValidationError,
    get_ledger_client,
    source_ref_for,
)

logger = logging.getLogger(__name__)


MAX_ATTEMPTS = 8
BATCH_SIZE = 50
LOOP_INTERVAL_SECONDS = 60  # corre cada minuto


async def run_retry_once() -> dict[str, int]:
    """Ejecuta un batch de reintentos. Retorna contadores para metricas."""
    stats = {"picked": 0, "recorded": 0, "still_failed": 0, "manual": 0, "errors": 0}

    async with SessionLocal() as db:
        # Audit fix: incluir tambien 'pending' (mensajes huerfanos por crash del
        # proceso entre marcar pending y completar el POST al ledger). Solo
        # tocar pending que tengan al menos 60s desde el ultimo intento, para
        # no competir con el request handler activo.
        rows = (await db.execute(text(f"""
            SELECT id::text AS id, tenant_id::text AS tenant_id,
                   channel, amount_cents_charged, currency,
                   ledger_request_id, ledger_attempts, sent_at, queued_at,
                   app_id, client_id, service_id,
                   template_slug, origin_kind, external_message_id, meta
            FROM messages
            WHERE ledger_status IN ('pending', 'failed')
              AND ledger_attempts < {MAX_ATTEMPTS}
              AND amount_cents_charged IS NOT NULL
              AND amount_cents_charged > 0
              AND status IN ('sent', 'delivered', 'bounced')
              AND (
                ledger_last_attempt_at IS NULL
                OR ledger_last_attempt_at < NOW() - INTERVAL '60 seconds'
              )
            ORDER BY ledger_last_attempt_at ASC NULLS FIRST
            LIMIT {BATCH_SIZE}
            FOR UPDATE SKIP LOCKED
        """))).mappings().all()

        stats["picked"] = len(rows)
        if not rows:
            return stats

        client = get_ledger_client()

        for row in rows:
            try:
                result = await _attempt_record(db, client, row)
                stats[result] = stats.get(result, 0) + 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "ledger_retry: error inesperado en msg %s -- %s", row["id"], exc
                )
                stats["errors"] += 1

        await db.commit()

    return stats


async def _attempt_record(
    db: AsyncSession,
    client: Any,
    row: dict[str, Any],
) -> str:
    """Reintenta una sola entry. Retorna 'recorded' | 'still_failed' | 'manual'."""
    message_id = row["id"]
    channel = row["channel"]
    request_id = row["ledger_request_id"] or source_ref_for(channel, message_id)

    # Reconstruir occurred_at (preferir sent_at, fallback queued_at).
    occurred = row["sent_at"] or row["queued_at"]
    if occurred is None:
        # Sin timestamp util: marcar manual.
        await _persist(db, message_id, request_id, "manual", None, "missing sent_at/queued_at")
        return "manual"
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    occurred_iso = occurred.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    caller_meta = dict(row.get("meta") or {})
    meta = {
        "app_id":     row["app_id"],
        "client_id":  row["client_id"],
        "service_id": row["service_id"],
        "template_id": row["template_slug"],
        "origin_kind": row["origin_kind"],
        "external_message_id": row["external_message_id"],
        **{k: v for k, v in caller_meta.items() if k not in ("app_id", "client_id", "service_id")},
    }
    description = (
        f"Retry {channel} msg {message_id[:8]} "
        f"via {row['template_slug'] or row['origin_kind'] or 'unknown'}"
    )

    new_status = "failed"
    entry_id: str | None = None
    last_error: str | None = None

    try:
        resp = await client.record_entry(
            source_ref=request_id,
            amount_cents=int(row["amount_cents_charged"]),
            currency=row["currency"],
            occurred_at_iso=occurred_iso,
            description=description,
            meta=meta,
        )
        entry_id = resp.get("id")
        new_status = "recorded"
    except LedgerTransientError as exc:
        new_status = "failed"
        last_error = f"transient: {exc}"
    except (LedgerAuthError, LedgerValidationError, LedgerError) as exc:
        new_status = "manual"
        last_error = f"fatal: {exc}"

    # Si llegamos al limite sin exito, escalar a manual.
    next_attempts = int(row["ledger_attempts"] or 0) + 1
    if new_status == "failed" and next_attempts >= MAX_ATTEMPTS:
        new_status = "manual"

    await _persist(db, message_id, request_id, new_status, entry_id, last_error)
    if new_status == "recorded":
        return "recorded"
    if new_status == "manual":
        return "manual"
    return "still_failed"


async def _persist(
    db: AsyncSession,
    message_id: str,
    request_id: str,
    new_status: str,
    entry_id: str | None,
    last_error: str | None,
) -> None:
    await db.execute(text("""
        UPDATE messages
           SET ledger_request_id = COALESCE(ledger_request_id, :rid),
               ledger_entry_id   = COALESCE(CAST(:eid AS uuid), ledger_entry_id),
               ledger_status     = :st,
               ledger_attempts   = ledger_attempts + 1,
               ledger_last_attempt_at = NOW(),
               ledger_last_error = :err,
               ledger_recorded_at = CASE WHEN :st = 'recorded' THEN NOW() ELSE ledger_recorded_at END
         WHERE id = CAST(:mid AS uuid)
    """), {
        "rid": request_id, "eid": entry_id, "st": new_status,
        "err": last_error, "mid": message_id,
    })


async def retry_loop_forever() -> None:
    """Loop infinito. Se cancela al shutdown. Errores no propagan — solo se loggean."""
    logger.info("ledger_retry: loop iniciado (interval=%ss)", LOOP_INTERVAL_SECONDS)
    while True:
        try:
            stats = await run_retry_once()
            if stats["picked"]:
                logger.info("ledger_retry batch: %s", stats)
        except asyncio.CancelledError:
            logger.info("ledger_retry: loop cancelado")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ledger_retry loop falla: %s", exc)
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)
