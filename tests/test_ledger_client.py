"""
test_ledger_client.py
=====================
Validaciones del LedgerClient sin red real.
"""

import pytest

from app.core.ledger_client import (
    LedgerClient,
    LedgerError,
    source_ref_for,
)


def test_source_ref_pattern():
    assert source_ref_for("email", "abc-123") == "msg-email-abc-123"
    assert source_ref_for("whatsapp", "xyz") == "msg-whatsapp-xyz"


async def test_record_entry_rechaza_amount_no_positivo():
    client = LedgerClient(base_url="http://nowhere.invalid", api_key="x")
    try:
        with pytest.raises(LedgerError):
            await client.record_entry(
                source_ref="msg-email-x",
                amount_cents=0,
                currency="MXN",
                occurred_at_iso="2026-05-25T14:00:00Z",
                description="x",
            )
        with pytest.raises(LedgerError):
            await client.record_entry(
                source_ref="msg-email-x",
                amount_cents=-1,
                currency="MXN",
                occurred_at_iso="2026-05-25T14:00:00Z",
                description="x",
            )
    finally:
        await client.aclose()


async def test_record_entry_rechaza_direction_invalido():
    client = LedgerClient(base_url="http://nowhere.invalid", api_key="x")
    try:
        with pytest.raises(LedgerError):
            await client.record_entry(
                source_ref="msg-email-x",
                amount_cents=10,
                currency="MXN",
                occurred_at_iso="2026-05-25T14:00:00Z",
                description="x",
                direction="invalid_dir",
            )
    finally:
        await client.aclose()
