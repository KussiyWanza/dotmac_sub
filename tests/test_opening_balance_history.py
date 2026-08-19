"""Complete-history opening targets for ADR 0007 universal cutover."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Numeric,
    Table,
    Uuid,
)

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.system_user import SystemUser
from app.services.billing.opening_balance_history import (
    SOURCE_TABLE,
    OpeningBalanceHistoryError,
    OpeningBalanceHistoryOrigin,
    OpeningBalanceHistoryQuery,
    OpeningBalanceSourceIdentityDisposition,
    OpeningBalanceSourceIdentityQuery,
    classify_opening_balance_source_identities,
    resolve_opening_balance_history_targets,
)
from app.services.carried_source_identity_adjudication import (
    OWNER as ADJUDICATION_OWNER,
)
from app.services.carried_source_identity_adjudication import (
    ConfirmCarriedSourceIdentityCommand,
    confirm_carried_source_identity_adjudication,
    preview_carried_source_identity_adjudication,
)
from app.services.owner_commands import CommandContext

HANDOFF = datetime(2026, 6, 18, tzinfo=UTC)
SNAPSHOT = datetime(2026, 8, 4, tzinfo=UTC)


@pytest.fixture
def history_table(db_session):  # noqa: ANN001
    metadata = MetaData()
    source = Table(
        SOURCE_TABLE,
        metadata,
        Column("splynx_customer_id", Integer, primary_key=True),
        Column("subscriber_id", Uuid(as_uuid=True)),
        Column("final_deposit", Numeric(19, 4), nullable=False),
        Column("active_transaction_net", Numeric(19, 4)),
        Column("active_transaction_rows", Integer, nullable=False),
        Column("transaction_reconciled", Boolean, nullable=False),
    )
    metadata.create_all(db_session.get_bind())
    try:
        yield source
    finally:
        db_session.rollback()
        metadata.drop_all(db_session.get_bind())


def _query(account_id) -> OpeningBalanceHistoryQuery:  # noqa: ANN001
    return OpeningBalanceHistoryQuery(
        account_ids=(account_id,),
        currency="NGN",
        native_after=HANDOFF,
        position_at=SNAPSHOT,
    )


def test_complete_history_net_plus_native_facts_is_the_target(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = 1001
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.execute(
        history_table.insert().values(
            splynx_customer_id=1001,
            subscriber_id=subscriber.id,
            final_deposit=Decimal("70.00"),
            active_transaction_net=Decimal("70.00"),
            active_transaction_rows=2,
            transaction_reconciled=True,
        )
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("20.00"),
            currency="NGN",
            memo="Post-Splynx native funding fact",
            effective_date=datetime(2026, 7, 1, tzinfo=UTC),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("999.00"),
            currency="NGN",
            memo="Future fact outside reviewed opening instant",
            effective_date=SNAPSHOT.replace(day=5),
            created_at=SNAPSHOT.replace(day=5),
        )
    )
    db_session.flush()

    snapshot = resolve_opening_balance_history_targets(
        db_session, _query(subscriber.id)
    )

    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert row.history_transaction_count == 2
    assert row.origin == OpeningBalanceHistoryOrigin.migrated_history
    assert row.history_position == Decimal("70.00")
    assert row.native_position == Decimal("20.00")
    assert row.target_position == Decimal("90.00")
    assert len(row.evidence_fingerprint) == 64
    assert len(snapshot.source_fingerprint) == 64


def test_complete_empty_history_is_mathematical_zero(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = 1002
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.execute(
        history_table.insert().values(
            splynx_customer_id=1002,
            subscriber_id=subscriber.id,
            final_deposit=Decimal("0.00"),
            active_transaction_net=None,
            active_transaction_rows=0,
            transaction_reconciled=False,
        )
    )
    db_session.flush()

    row = resolve_opening_balance_history_targets(
        db_session, _query(subscriber.id)
    ).rows[0]

    assert row.history_transaction_count == 0
    assert row.history_position == Decimal("0.00")
    assert row.target_position == Decimal("0.00")


def test_native_account_after_handoff_has_explicit_zero_history(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = None
    subscriber.created_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("15.00"),
            currency="NGN",
            memo="Native funding fact",
            effective_date=datetime(2026, 7, 2, tzinfo=UTC),
            created_at=datetime(2026, 7, 2, tzinfo=UTC),
        )
    )
    db_session.flush()

    row = resolve_opening_balance_history_targets(
        db_session, _query(subscriber.id)
    ).rows[0]

    assert row.origin == OpeningBalanceHistoryOrigin.native_after_handoff
    assert row.splynx_customer_id is None
    assert row.history_position == Decimal("0.00")
    assert row.native_position == Decimal("15.00")
    assert row.target_position == Decimal("15.00")


def test_migrated_account_without_splynx_identity_fails_the_complete_snapshot(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = None
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.flush()

    identity = classify_opening_balance_source_identities(
        db_session,
        OpeningBalanceSourceIdentityQuery(
            account_ids=(subscriber.id,),
            native_after=HANDOFF,
            position_at=SNAPSHOT,
        ),
    )

    assert identity.unresolved_account_ids == (subscriber.id,)
    assert (
        identity.rows[0].disposition
        is OpeningBalanceSourceIdentityDisposition.unresolved_carried_identity
    )
    assert len(identity.rows[0].evidence_fingerprint) == 64

    with pytest.raises(OpeningBalanceHistoryError) as exc:
        resolve_opening_balance_history_targets(db_session, _query(subscriber.id))

    assert exc.value.code.endswith("source_cohort_incomplete")
    assert exc.value.details["reason"] == "missing_carried_source_identity"


def test_reviewed_native_account_before_handoff_uses_complete_sub_history(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = None
    subscriber.created_at = datetime(2026, 5, 22, tzinfo=UTC)
    subscriber.crm_subscriber_id = uuid4()
    subscriber.metadata_ = {
        "source": "dotmac_omni",
        "crm_person_id": str(uuid4()),
        "crm_project_id": str(uuid4()),
        "crm_quote_id": str(uuid4()),
        "crm_sales_order_id": str(uuid4()),
    }
    reviewed_by = SystemUser(
        first_name="Billing",
        last_name="Reviewer",
        email=f"opening-review-{uuid4().hex}@example.com",
    )
    approved_by = SystemUser(
        first_name="Finance",
        last_name="Approver",
        email=f"opening-approve-{uuid4().hex}@example.com",
    )
    db_session.add_all((reviewed_by, approved_by))
    db_session.commit()
    preview = preview_carried_source_identity_adjudication(db_session, subscriber.id)
    command = ConfirmCarriedSourceIdentityCommand(
        context=CommandContext.system(
            actor="pytest:billing-migration",
            scope=ADJUDICATION_OWNER,
            reason="Reviewed native provenance for complete-history opening.",
            idempotency_key="opening-native-before-handoff:test",
        ),
        account_id=subscriber.id,
        expected_preview_fingerprint=preview.fingerprint,
        evidence_ref="finance-review:opening-history/test",
        evidence_sha256="b" * 64,
        reviewed_by_id=reviewed_by.id,
        approved_by_id=approved_by.id,
    )
    db_session.rollback()
    outcome = confirm_carried_source_identity_adjudication(db_session, command)
    db_session.add_all(
        (
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="Native funding before handoff",
                effective_date=datetime(2026, 6, 1, tzinfo=UTC),
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("15.00"),
                currency="NGN",
                memo="Native funding after handoff",
                effective_date=datetime(2026, 7, 1, tzinfo=UTC),
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        )
    )
    db_session.flush()

    identity = classify_opening_balance_source_identities(
        db_session,
        OpeningBalanceSourceIdentityQuery(
            account_ids=(subscriber.id,),
            native_after=HANDOFF,
            position_at=SNAPSHOT,
        ),
    )
    row = resolve_opening_balance_history_targets(
        db_session, _query(subscriber.id)
    ).rows[0]

    assert (
        identity.rows[0].disposition
        is OpeningBalanceSourceIdentityDisposition.native_before_handoff
    )
    assert identity.rows[0].adjudication_id == outcome.decision_id
    assert row.origin is OpeningBalanceHistoryOrigin.native_before_handoff
    assert row.adjudication_id == outcome.decision_id
    assert row.splynx_customer_id is None
    assert row.history_position == Decimal("0.00")
    assert row.native_position == Decimal("25.00")
    assert row.target_position == Decimal("25.00")


def test_missing_source_customer_fails_the_complete_snapshot(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = 1003
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.flush()

    with pytest.raises(OpeningBalanceHistoryError) as exc:
        resolve_opening_balance_history_targets(db_session, _query(subscriber.id))

    assert exc.value.code.endswith("source_cohort_incomplete")


def test_unreconciled_transaction_net_fails_the_complete_snapshot(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = 1004
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.execute(
        history_table.insert().values(
            splynx_customer_id=1004,
            subscriber_id=subscriber.id,
            final_deposit=Decimal("75.00"),
            active_transaction_net=Decimal("70.00"),
            active_transaction_rows=2,
            transaction_reconciled=False,
        )
    )
    db_session.flush()

    with pytest.raises(OpeningBalanceHistoryError) as exc:
        resolve_opening_balance_history_targets(db_session, _query(subscriber.id))

    assert exc.value.code.endswith("source_history_unreconciled")


def test_same_target_with_changed_source_evidence_changes_the_fingerprint(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = 1005
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.execute(
        history_table.insert().values(
            splynx_customer_id=1005,
            subscriber_id=subscriber.id,
            final_deposit=Decimal("70.00"),
            active_transaction_net=Decimal("70.00"),
            active_transaction_rows=2,
            transaction_reconciled=True,
        )
    )
    db_session.flush()

    first = resolve_opening_balance_history_targets(db_session, _query(subscriber.id))
    db_session.execute(
        history_table.update()
        .where(history_table.c.splynx_customer_id == 1005)
        .values(active_transaction_rows=3)
    )
    second = resolve_opening_balance_history_targets(db_session, _query(subscriber.id))

    assert first.rows[0].target_position == second.rows[0].target_position
    assert first.rows[0].evidence_fingerprint != second.rows[0].evidence_fingerprint
    assert first.source_fingerprint != second.source_fingerprint


def test_duplicate_source_rows_for_one_customer_fail_the_complete_snapshot(
    db_session, subscriber, history_table
):
    subscriber.splynx_customer_id = 1006
    subscriber.created_at = datetime(2026, 6, 1, tzinfo=UTC)
    db_session.execute(
        history_table.insert(),
        [
            {
                "splynx_customer_id": 1006,
                "subscriber_id": subscriber.id,
                "final_deposit": Decimal("10.00"),
                "active_transaction_net": Decimal("10.00"),
                "active_transaction_rows": 1,
                "transaction_reconciled": True,
            },
            {
                "splynx_customer_id": 1007,
                "subscriber_id": subscriber.id,
                "final_deposit": Decimal("10.00"),
                "active_transaction_net": Decimal("10.00"),
                "active_transaction_rows": 1,
                "transaction_reconciled": True,
            },
        ],
    )
    db_session.flush()

    with pytest.raises(OpeningBalanceHistoryError) as exc:
        resolve_opening_balance_history_targets(db_session, _query(subscriber.id))

    assert exc.value.code.endswith("source_identity_duplicate")
