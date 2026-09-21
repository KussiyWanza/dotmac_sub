"""Dual-session PostgreSQL evidence for the additive keyset cursor.

``tests/test_invoice_accounting_sync_v2.py`` proves the cursor predicate's
shape against a single SQLite session — useful for pinning the query logic,
but not evidence about what a genuinely CONCURRENT writer does, since a
same-session ``flush()`` never crosses a transaction boundary. This module
is the real-concurrency counterpart: a reader session mid-walk, and a
SEPARATE writer session that independently commits, on a real migrated
PostgreSQL database (see ``tests/integration/test_erp_sync_admission_postgres
.py`` for the same ``Session(engine)`` two-session pattern).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber
from app.services.dotmac_erp.invoice_sync_projection import (
    InvoiceAccountingSyncQuery,
    list_invoice_accounting_sync,
)


def _invoice(account_id, *, invoice_number: str, updated_at: datetime) -> Invoice:
    return Invoice(
        account_id=account_id,
        invoice_number=invoice_number,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
        issued_at=updated_at,
        updated_at=updated_at,
        is_active=True,
    )


def test_keyset_walk_does_not_skip_a_stationary_row_under_a_real_concurrent_writer(
    engine,
):
    """A reader session's keyset walk is already past A/B when a genuinely
    SEPARATE, independently-committing writer session updates A so it
    re-sorts past D. The reader's next page must still return the untouched
    row C (the concrete OFFSET-paging hazard this cursor replaces), and must
    correctly pick up A's committed new revision later in the walk (intended
    per Decision A — the downstream consumer is idempotent on the invoice id
    plus its source ``updated_at``, so a fresh revision is a valid new
    observation, not a duplicate)."""
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]

    with session_factory() as setup:
        reseller = Reseller(
            name=f"Keyset PG {suffix}",
            code=f"keyset-pg-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Keyset",
            last_name="Concurrency",
            email=f"keyset-concurrency-{suffix}@example.com",
            reseller=reseller,
        )
        setup.add_all([reseller, account])
        setup.flush()

        t0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        t1 = t0 + timedelta(minutes=1)
        t2 = t0 + timedelta(minutes=2)
        t3 = t0 + timedelta(minutes=3)
        row_a = _invoice(account.id, invoice_number=f"INV-PG-A-{suffix}", updated_at=t0)
        row_b = _invoice(account.id, invoice_number=f"INV-PG-B-{suffix}", updated_at=t1)
        row_c = _invoice(account.id, invoice_number=f"INV-PG-C-{suffix}", updated_at=t2)
        row_d = _invoice(account.id, invoice_number=f"INV-PG-D-{suffix}", updated_at=t3)
        setup.add_all([row_a, row_b, row_c, row_d])
        setup.commit()
        account_id = account.id
        row_a_id = row_a.id
        row_b_id = row_b.id
        row_c_id = row_c.id
        row_d_id = row_d.id

    def _query(**overrides) -> InvoiceAccountingSyncQuery:
        values: dict = {
            "invoice_id": None,
            "account_id": account_id,
            "status": None,
            "is_active": None,
            "updated_since": None,
            "limit": 2,
            "offset": 0,
            "after_updated_at": None,
            "after_id": None,
        }
        values.update(overrides)
        return InvoiceAccountingSyncQuery(**values)

    reader = session_factory()
    writer = session_factory()
    try:
        page1 = list_invoice_accounting_sync(reader, _query())
        assert [item.source_invoice_id for item in page1.items] == [
            row_a_id,
            row_b_id,
        ]
        cursor = page1.items[-1]

        # A SEPARATE, independently-committing writer session: the real
        # concurrency this property depends on. A same-session flush would
        # not exercise cross-transaction visibility at all.
        t4 = t3 + timedelta(minutes=1)
        writer_row_a = writer.get(Invoice, row_a_id)
        writer_row_a.updated_at = t4
        writer.add(writer_row_a)
        writer.commit()

        page2 = list_invoice_accounting_sync(
            reader,
            _query(
                after_updated_at=cursor.updated_at,
                after_id=cursor.source_invoice_id,
            ),
        )
        # C is stationary (its own (updated_at, id) never changed) and must
        # not be skipped, regardless of the writer's committed update to A.
        assert [item.source_invoice_id for item in page2.items] == [
            row_c_id,
            row_d_id,
        ]

        cursor2 = page2.items[-1]
        page3 = list_invoice_accounting_sync(
            reader,
            _query(
                after_updated_at=cursor2.updated_at,
                after_id=cursor2.source_invoice_id,
            ),
        )
        # A reappears with its writer-committed new revision: intended per
        # Decision A, not a duplicate (A was never returned at this later
        # walk position before).
        assert [item.source_invoice_id for item in page3.items] == [row_a_id]
        assert page3.items[0].updated_at.astimezone(UTC) == t4
    finally:
        reader.close()
        writer.close()
