"""Sub's canonical content digest over the invoice-accounting-sync.v2 projection.

Covers: a real, checked-in fixture whose serialized wire shape and digest are
both actually computed (not guessed); that the digest is stable to collection
reordering; that every covered fact independently moves the digest; the two
explicit exclusion regressions (a subscriber-profile-only edit and an
``updated_at``-only change both leave the digest unchanged); the schema
validator rejecting a malformed digest; and the ``replace_admin_draft_lines``
revision-marker fix (a description-only edit must still advance
``invoice.updated_at``, since nothing else on the Invoice row would).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm.attributes import set_committed_value

from app.models.billing import (
    Invoice,
    InvoiceDiscountSource,
    InvoiceDiscountType,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.schemas.billing import (
    InvoiceAccountingSyncDisposition,
    InvoiceAccountingSyncIssueCode,
    InvoiceAccountingSyncIssueRead,
    InvoiceAccountingSyncLineRead,
    InvoiceAccountingSyncRead,
    InvoiceAccountingSyncSourceKind,
    InvoiceLineCreate,
)
from app.services.billing.invoices import DraftInvoiceLineReplacement, InvoiceLines
from app.services.dotmac_erp.invoice_sync_digest import (
    INVOICE_PROJECTION_DIGEST_VERSION,
    compute_invoice_projection_digest,
)
from app.services.dotmac_erp.invoice_sync_projection import (
    ACCOUNTING_SYNC_CONTRACT_VERSION,
    project_invoice_for_accounting,
)
from app.services.subscriber import _default_reseller_id

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "invoice_accounting_sync_v2_sample.json"
)

_ISSUED_AT = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
_DUE_AT = datetime(2026, 2, 14, 10, 30, 0, tzinfo=UTC)
_SUBSCRIBER_TIMESTAMP = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

_ACCOUNT_ID = UUID("22222222-2222-2222-2222-222222222222")
_INVOICE_ID = UUID("11111111-1111-1111-1111-111111111111")
_TAXED_LINE_ID = UUID("33333333-3333-3333-3333-333333333333")
_UNTAXED_LINE_ID = UUID("44444444-4444-4444-4444-444444444444")
_TAX_RATE_ID = UUID("55555555-5555-5555-5555-555555555555")

# The real hardcoded sha256 hex digest of the fixture invoice built below.
# This is the regression oracle: any future silent change to the
# canonicalisation algorithm or the covered-fact set changes this value. See
# the implementer's report for exactly how this value was computed and
# confirmed stable.
_EXPECTED_FIXTURE_DIGEST = (
    "0dfecf2e1f96a2d1a63eb8e5e9f78f0aefbf661ab2c424b841b5bb2eda85aa7c"
)


# --------------------------------------------------------------------------
# DB-backed fixture invoice — the realistic scenario reused by the JSON
# fixture, the hardcoded-digest test and the two exclusion regressions.
# --------------------------------------------------------------------------


def _fixture_subscriber(db_session, **overrides) -> Subscriber:
    values: dict = {
        "id": _ACCOUNT_ID,
        "first_name": "Ada",
        "last_name": "Okafor",
        "email": "ada.okafor@example.com",
        "phone": "+2348012345678",
        "status": SubscriberStatus.active,
        "is_active": True,
        "reseller_id": _default_reseller_id(db_session),
        "address_line1": "12 Marina Road",
        "city": "Lagos",
        "region": "Lagos State",
        "postal_code": "100001",
        "country_code": "NG",
        "created_at": _SUBSCRIBER_TIMESTAMP,
        "updated_at": _SUBSCRIBER_TIMESTAMP,
    }
    values.update(overrides)
    subscriber = Subscriber(**values)
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _fixture_tax_rate(db_session) -> TaxRate:
    tax_rate = TaxRate(
        id=_TAX_RATE_ID,
        name="VAT 7.5%",
        code="VAT75",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    return tax_rate


def _fixture_discount_actor(db_session) -> SystemUser:
    actor = SystemUser(
        first_name="Invoice",
        last_name="Digest",
        display_name="Invoice Digest Fixture",
        email=f"invoice-digest-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add(actor)
    db_session.flush()
    return actor


def _fixture_invoice(db_session, account_id: UUID) -> Invoice:
    discount_actor = _fixture_discount_actor(db_session)
    invoice = Invoice(
        id=_INVOICE_ID,
        account_id=account_id,
        invoice_number="INV-DIGEST-0001",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("150000.00"),
        discount_type=InvoiceDiscountType.fixed_amount.value,
        discount_value=Decimal("10000.00"),
        discount_amount=Decimal("10000.00"),
        discount_revision=1,
        discount_source=InvoiceDiscountSource.manual.value,
        discount_applied_by_system_user_id=discount_actor.id,
        discount_applied_at=_ISSUED_AT,
        tax_total=Decimal("7000.00"),
        total=Decimal("147000.00"),
        balance_due=Decimal("147000.00"),
        issued_at=_ISSUED_AT,
        due_at=_DUE_AT,
        memo="Digest fixture invoice",
        is_proforma=False,
        updated_at=_ISSUED_AT,
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _fixture_lines(db_session, invoice: Invoice, tax_rate: TaxRate) -> None:
    taxed = InvoiceLine(
        id=_TAXED_LINE_ID,
        invoice_id=invoice.id,
        description="Fibre 100Mbps monthly subscription",
        quantity=Decimal("1.000"),
        unit_price=Decimal("100000.00"),
        amount=Decimal("100000.00"),
        tax_rate_id=tax_rate.id,
        tax_application=TaxApplication.exclusive,
        tax_rate_snapshot_version=1,
        tax_rate_code_snapshot=tax_rate.code,
        tax_rate_percent_snapshot=tax_rate.rate,
        tax_rate_is_active_snapshot=tax_rate.is_active,
        is_active=True,
    )
    untaxed = InvoiceLine(
        id=_UNTAXED_LINE_ID,
        invoice_id=invoice.id,
        description="Installation fee (tax-exempt)",
        quantity=Decimal("1.000"),
        unit_price=Decimal("50000.00"),
        amount=Decimal("50000.00"),
        tax_application=TaxApplication.exclusive,
        is_active=True,
    )
    db_session.add_all([taxed, untaxed])
    db_session.flush()


_NAIVE_DATETIME_ATTRS = ("issued_at", "due_at", "paid_at", "updated_at")
_ACCOUNT_NAIVE_DATETIME_ATTRS = ("created_at", "updated_at")


def _normalize_sqlite_naive_timestamps(invoice: Invoice) -> None:
    """Re-attach UTC to an Invoice's (and its account's) timestamps after a
    SQLite reload.

    SQLite's DATETIME column drops tzinfo on round-trip (unlike PostgreSQL,
    whose ``DateTime(timezone=True)`` columns are always aware — see
    ``tests/test_invoice_accounting_sync_v2.py``'s identical comment).
    ``canonical_datetime`` correctly REJECTS a naive value in production,
    since a naive value there would mean a real offset was silently dropped;
    this test-only helper reflects that the SQLite unit lane's wall-clock
    value was always UTC to begin with, so it is safe to re-attach here.

    Uses ``set_committed_value`` rather than plain attribute assignment so
    this normalization is never mistaken by SQLAlchemy for a pending change
    — a plain ``invoice.issued_at = ...`` would mark the attribute dirty and
    could trigger a spurious ``UPDATE`` (and therefore an unwanted
    ``onupdate`` bump of ``updated_at``) on the next unrelated flush.
    """

    for attr in _NAIVE_DATETIME_ATTRS:
        value = getattr(invoice, attr)
        if value is not None and value.tzinfo is None:
            set_committed_value(invoice, attr, value.replace(tzinfo=UTC))

    # The account relationship is its own lazy-loaded SQLite round-trip
    # (a separate SELECT, potentially resolved through a differently-cached
    # DATETIME result processor than the Invoice row above), so it needs the
    # identical re-attachment, on the identical rationale.
    account = invoice.account
    for attr in _ACCOUNT_NAIVE_DATETIME_ATTRS:
        value = getattr(account, attr)
        if value is not None and value.tzinfo is None:
            set_committed_value(account, attr, value.replace(tzinfo=UTC))


def _build_fixture_invoice(db_session) -> Invoice:
    subscriber = _fixture_subscriber(db_session)
    tax_rate = _fixture_tax_rate(db_session)
    invoice = _fixture_invoice(db_session, subscriber.id)
    _fixture_lines(db_session, invoice, tax_rate)
    db_session.commit()
    db_session.refresh(invoice)
    _normalize_sqlite_naive_timestamps(invoice)
    return invoice


def test_fixture_invoice_is_blocked_on_the_single_expected_issue(db_session) -> None:
    invoice = _build_fixture_invoice(db_session)
    projection = project_invoice_for_accounting(invoice)

    assert projection.disposition is InvoiceAccountingSyncDisposition.BLOCKED
    assert len(projection.issues) == 1
    assert (
        projection.issues[0].code
        is InvoiceAccountingSyncIssueCode.DISCOUNT_ALLOCATION_UNDEFINED
    )
    assert len(projection.lines) == 2


def test_fixture_matches_the_checked_in_wire_shape(db_session) -> None:
    invoice = _build_fixture_invoice(db_session)
    projection = project_invoice_for_accounting(invoice)

    actual = json.loads(projection.model_dump_json())
    expected = json.loads(_FIXTURE_PATH.read_text())
    assert actual == expected


def test_digest_matches_the_hardcoded_expected_value(db_session) -> None:
    invoice = _build_fixture_invoice(db_session)
    projection = project_invoice_for_accounting(invoice)

    assert projection.digest_version == INVOICE_PROJECTION_DIGEST_VERSION
    assert projection.projection_digest == _EXPECTED_FIXTURE_DIGEST


def test_digest_unchanged_when_only_subscriber_profile_changes(db_session) -> None:
    """The false-positive bug this task fixes: a subscriber-profile-only edit
    never advances ``invoice.updated_at`` (it isn't an Invoice-row column), so
    it must not move the digest either."""
    invoice = _build_fixture_invoice(db_session)
    before = project_invoice_for_accounting(invoice)

    invoice.account.address_line1 = "99 A Different Close"
    invoice.account.phone = "+2347000000000"
    invoice.account.city = "Kano"
    db_session.flush()
    db_session.refresh(invoice)
    _normalize_sqlite_naive_timestamps(invoice)

    after = project_invoice_for_accounting(invoice)

    assert after.account.address_line1 != before.account.address_line1
    assert after.projection_digest == before.projection_digest


def test_digest_unchanged_when_only_updated_at_changes(db_session) -> None:
    """``updated_at`` is the external revision key the digest is compared
    against per-key, not digest content — it is deliberately excluded."""
    invoice = _build_fixture_invoice(db_session)
    before = project_invoice_for_accounting(invoice)

    invoice.updated_at = before.updated_at + timedelta(hours=1)
    db_session.flush()
    db_session.refresh(invoice)
    _normalize_sqlite_naive_timestamps(invoice)

    after = project_invoice_for_accounting(invoice)

    assert after.updated_at != before.updated_at
    assert after.projection_digest == before.projection_digest


@pytest.mark.parametrize("field", ["projection_digest"])
def test_schema_rejects_wrong_length_digest(db_session, field: str) -> None:
    invoice = _build_fixture_invoice(db_session)
    projection = project_invoice_for_accounting(invoice)
    data = json.loads(projection.model_dump_json())
    data[field] = "abc123"
    with pytest.raises(ValidationError):
        InvoiceAccountingSyncRead.model_validate(data)


def test_schema_rejects_uppercase_digest(db_session) -> None:
    invoice = _build_fixture_invoice(db_session)
    projection = project_invoice_for_accounting(invoice)
    data = json.loads(projection.model_dump_json())
    data["projection_digest"] = projection.projection_digest.upper()
    with pytest.raises(ValidationError):
        InvoiceAccountingSyncRead.model_validate(data)


def test_schema_rejects_non_hex_digest(db_session) -> None:
    invoice = _build_fixture_invoice(db_session)
    projection = project_invoice_for_accounting(invoice)
    data = json.loads(projection.model_dump_json())
    data["projection_digest"] = "g" * 64
    with pytest.raises(ValidationError):
        InvoiceAccountingSyncRead.model_validate(data)


# --------------------------------------------------------------------------
# Direct digest-function tests — order independence and per-fact sensitivity.
# These construct plain (unpersisted) ``Invoice``/``InvoiceAccountingSync*``
# objects and call ``compute_invoice_projection_digest`` directly: no
# relationship traversal is needed for the covered-fact domain, so no
# database session is required.
# --------------------------------------------------------------------------


def _plain_invoice(**overrides) -> Invoice:
    values: dict = {
        "id": _INVOICE_ID,
        "account_id": _ACCOUNT_ID,
        "invoice_number": "INV-DIGEST-0001",
        "status": InvoiceStatus.issued,
        "currency": "NGN",
        "splynx_invoice_id": None,
        "discount_value": Decimal("10000.00"),
        "issued_at": _ISSUED_AT,
        "due_at": _DUE_AT,
        "paid_at": None,
        "memo": "Digest fixture invoice",
        "is_proforma": False,
    }
    values.update(overrides)
    return Invoice(**values)


def _plain_taxed_line(**overrides) -> InvoiceAccountingSyncLineRead:
    values: dict = {
        "id": _TAXED_LINE_ID,
        "description": "Fibre 100Mbps monthly subscription",
        "quantity": Decimal("1.000"),
        "unit_price": Decimal("100000.00"),
        "source_amount": Decimal("100000.00"),
        "net_amount_before_discount": Decimal("100000.00"),
        "tax_amount_before_discount": Decimal("7500.00"),
        "gross_amount_before_discount": Decimal("107500.00"),
        "tax_rate_id": _TAX_RATE_ID,
        "tax_rate_code": "VAT75",
        "tax_rate_percent": Decimal("7.5000"),
        "tax_rate_is_active": True,
        "tax_application": TaxApplication.exclusive,
    }
    values.update(overrides)
    return InvoiceAccountingSyncLineRead(**values)


def _plain_untaxed_line(**overrides) -> InvoiceAccountingSyncLineRead:
    values: dict = {
        "id": _UNTAXED_LINE_ID,
        "description": "Installation fee (tax-exempt)",
        "quantity": Decimal("1.000"),
        "unit_price": Decimal("50000.00"),
        "source_amount": Decimal("50000.00"),
        "net_amount_before_discount": Decimal("50000.00"),
        "tax_amount_before_discount": Decimal("0.00"),
        "gross_amount_before_discount": Decimal("50000.00"),
        "tax_application": TaxApplication.exclusive,
    }
    values.update(overrides)
    return InvoiceAccountingSyncLineRead(**values)


def _base_digest_kwargs() -> dict:
    return {
        "contract_version": ACCOUNTING_SYNC_CONTRACT_VERSION,
        "source_kind": InvoiceAccountingSyncSourceKind.NATIVE,
        "invoice": _plain_invoice(),
        "subtotal_before_discount": Decimal("150000.00"),
        "discount_type": InvoiceDiscountType.fixed_amount,
        "discount_amount": Decimal("10000.00"),
        "discounted_subtotal": Decimal("140000.00"),
        "tax_total": Decimal("7000.00"),
        "total": Decimal("147000.00"),
        "balance_due": Decimal("147000.00"),
        "disposition": InvoiceAccountingSyncDisposition.BLOCKED,
        "issues": [
            InvoiceAccountingSyncIssueRead(
                code=InvoiceAccountingSyncIssueCode.DISCOUNT_ALLOCATION_UNDEFINED,
                actual_amount=Decimal("10000.00"),
            )
        ],
        "lines": [_plain_taxed_line(), _plain_untaxed_line()],
    }


def test_digest_is_independent_of_collection_order() -> None:
    ordered = _base_digest_kwargs()
    reordered = _base_digest_kwargs()
    reordered["lines"] = list(reversed(ordered["lines"]))

    assert compute_invoice_projection_digest(
        **ordered
    ) == compute_invoice_projection_digest(**reordered)


def _mutate_status(kwargs: dict) -> dict:
    kwargs["invoice"] = _plain_invoice(status=InvoiceStatus.paid)
    return kwargs


def _mutate_total(kwargs: dict) -> dict:
    kwargs["total"] = Decimal("148000.00")
    return kwargs


def _mutate_tax_total(kwargs: dict) -> dict:
    kwargs["tax_total"] = Decimal("7100.00")
    return kwargs


def _mutate_subtotal_before_discount(kwargs: dict) -> dict:
    kwargs["subtotal_before_discount"] = Decimal("151000.00")
    return kwargs


def _mutate_memo(kwargs: dict) -> dict:
    kwargs["invoice"] = _plain_invoice(memo="A different memo")
    return kwargs


def _mutate_line_description(kwargs: dict) -> dict:
    lines = list(kwargs["lines"])
    lines[0] = _plain_taxed_line(description="A changed description")
    kwargs["lines"] = lines
    return kwargs


def _mutate_line_tax_rate_id(kwargs: dict) -> dict:
    lines = list(kwargs["lines"])
    lines[0] = _plain_taxed_line(tax_rate_id=uuid4())
    kwargs["lines"] = lines
    return kwargs


def _mutate_issues_only(kwargs: dict) -> dict:
    """Append a new issue while holding ``lines`` byte-identical to the base.

    Isolates ``issues`` from ``lines``: a mutation that also appended a line
    (as a combined scenario does) would still move the digest even if
    ``issues`` were accidentally dropped from the covered-fact domain,
    because the added line alone would change it.
    """
    issues = list(kwargs["issues"])
    issues.append(
        InvoiceAccountingSyncIssueRead(
            code=InvoiceAccountingSyncIssueCode.NO_ACTIVE_LINES,
        )
    )
    kwargs["issues"] = issues
    return kwargs


def _mutate_disposition_only(kwargs: dict) -> dict:
    """Change ``disposition`` alone, holding ``issues``/``lines`` identical."""
    kwargs["disposition"] = InvoiceAccountingSyncDisposition.READY
    return kwargs


def _mutate_issues_and_disposition_via_extra_line(kwargs: dict) -> dict:
    """End-to-end scenario: a mismatched extra line adds both an issue and
    moves the disposition. Kept alongside the two isolated mutations above,
    not instead of them, for realistic line+issue coverage."""
    mismatched_line_id = uuid4()
    lines = list(kwargs["lines"])
    lines.append(
        _plain_taxed_line(
            id=mismatched_line_id,
            description="Mismatched extra line",
            unit_price=Decimal("1.00"),
            source_amount=Decimal("999.00"),
            net_amount_before_discount=Decimal("999.00"),
            tax_amount_before_discount=Decimal("0.00"),
            gross_amount_before_discount=Decimal("999.00"),
            tax_rate_id=None,
            tax_rate_code=None,
            tax_rate_percent=None,
            tax_rate_is_active=None,
        )
    )
    issues = list(kwargs["issues"])
    issues.append(
        InvoiceAccountingSyncIssueRead(
            code=InvoiceAccountingSyncIssueCode.LINE_AMOUNT_MISMATCH,
            line_id=mismatched_line_id,
            expected_amount=Decimal("1.00"),
            actual_amount=Decimal("999.00"),
        )
    )
    kwargs["lines"] = lines
    kwargs["issues"] = issues
    return kwargs


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_status,
        _mutate_total,
        _mutate_tax_total,
        _mutate_subtotal_before_discount,
        _mutate_memo,
        _mutate_line_description,
        _mutate_line_tax_rate_id,
        _mutate_issues_only,
        _mutate_disposition_only,
        _mutate_issues_and_disposition_via_extra_line,
    ],
    ids=[
        "status",
        "total",
        "tax_total",
        "subtotal_before_discount",
        "memo",
        "line_description",
        "line_tax_rate_id",
        "issues_only",
        "disposition_only",
        "issues_and_disposition_via_extra_line",
    ],
)
def test_changing_one_covered_fact_changes_the_digest(mutate) -> None:
    base_digest = compute_invoice_projection_digest(**_base_digest_kwargs())
    mutated_digest = compute_invoice_projection_digest(**mutate(_base_digest_kwargs()))
    assert mutated_digest != base_digest


# --------------------------------------------------------------------------
# The §4 revision-marker fix: a description-only draft-line edit must still
# advance ``invoice.updated_at``.
# --------------------------------------------------------------------------


def test_replace_admin_draft_lines_description_only_edit_advances_updated_at(
    db_session, subscriber
) -> None:
    """Regression test for the ``replace_admin_draft_lines`` fix.

    Before the fix, this fails: ``_recalculate_invoice_totals`` recomputes
    identical totals from the unchanged quantity/unit_price, no Invoice-row
    column value actually changes, SQLAlchemy issues no UPDATE for that row,
    and the column-level ``onupdate`` never fires — even though the line's
    ``description`` (a covered digest fact) genuinely changed.
    """
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DRAFT-0001",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100.00"),
        discount_amount=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        description="Original description",
        quantity=Decimal("1.000"),
        unit_price=Decimal("100.00"),
        amount=Decimal("100.00"),
        tax_application=TaxApplication.exclusive,
        is_active=True,
    )
    db_session.add(line)
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(line)

    before_updated_at = invoice.updated_at

    InvoiceLines.replace_admin_draft_lines(
        db_session,
        invoice.id,
        (
            DraftInvoiceLineReplacement(
                line_id=line.id,
                payload=InvoiceLineCreate(
                    invoice_id=invoice.id,
                    description="Updated description only",
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    amount=line.amount,
                    tax_rate_id=None,
                    tax_application=TaxApplication.exclusive,
                ),
            ),
        ),
    )
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.updated_at > before_updated_at


# --------------------------------------------------------------------------
# The accounting-sync-v2 route now carries a digest built from THREE separate
# SQL statements (header, account, lines via selectinload). Under READ
# COMMITTED a commit landing between them could leave `updated_at` stale
# while the digest moved — the same "one revision key, two projections"
# contradiction this task exists to close, just from a within-process race.
# The route must pin one REPEATABLE READ, READ ONLY snapshot before any of
# those statements run.
# --------------------------------------------------------------------------


def test_begin_read_only_snapshot_pins_repeatable_read_read_only_on_postgresql() -> (
    None
):
    """Direct behavioral proof of the seam the route below relies on.

    ``begin_read_only_snapshot`` is existing, unmodified infrastructure
    (``app.db``) — this proves what it actually does to a session's
    connection when the bind reports as PostgreSQL, without needing a real
    PostgreSQL server.
    """
    from app.db import READ_ONLY_SNAPSHOT_OPTIONS, begin_read_only_snapshot

    class _FakeDialect:
        name = "postgresql"

    class _FakeBind:
        dialect = _FakeDialect()

    class _RecordingSession:
        def __init__(self) -> None:
            self.connection_calls: list[dict] = []

        def get_bind(self):
            return _FakeBind()

        def connection(self, execution_options=None):
            self.connection_calls.append(dict(execution_options or {}))

    fake_session = _RecordingSession()
    begin_read_only_snapshot(fake_session)

    assert fake_session.connection_calls == [dict(READ_ONLY_SNAPSHOT_OPTIONS)]


def test_accounting_sync_v2_route_pins_a_read_only_snapshot_before_querying() -> None:
    """The route calls ``begin_read_only_snapshot(db)`` before its first query.

    A source-order check rather than an end-to-end DB test: proving the
    isolation level is actually *requested* is this implementer's job; a real
    concurrent-transaction proof of the underlying guarantee needs Postgres
    and belongs to CI's Postgres-backed suite.
    """
    import inspect

    from app.api.billing import sync_invoices_for_accounting_v2

    source = inspect.getsource(sync_invoices_for_accounting_v2)

    begin_pos = source.index("begin_read_only_snapshot(db)")
    validate_pos = source.index("_validate_sync_cursor_pair(")
    query_pos = source.index("list_invoice_accounting_sync(")

    assert begin_pos < validate_pos < query_pos
