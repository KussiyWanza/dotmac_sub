from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.billing_contract import BillingRecordAuthority
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.customer_subledger import (
    CustomerPostingGroup,
    CustomerSubledgerOpeningPosition,
    PositionEffectKind,
    PostingCommandKind,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import customer_financial_ledger
from app.services.billing.customer_subledger import resolve_position
from app.services.billing.shadow_verification import (
    BillingShadowVerification,
    BillingShadowVerificationError,
    Phase3OpeningPreviewResult,
    RecordPhase3OpeningPreviewCommand,
    RecordPhase3SubledgerParityCommand,
    RecordPostCutoverAccountOpeningPreviewCommand,
    RecordPostCutoverMigratedAccountOpeningPreviewCommand,
    ReviewedMigratedOpeningSource,
    record_phase3_opening_preview,
    record_phase3_subledger_parity,
    record_post_cutover_account_opening_preview,
    record_post_cutover_migrated_account_opening_preview,
)
from app.services.billing.subledger_opening import (
    ActivateCustomerSubledgerAuthorityCommand,
    CaptureCustomerSubledgerOpeningsCommand,
    CustomerSubledgerOpeningError,
    activate_customer_subledger_authority,
    capture_customer_subledger_opening_positions,
)
from app.services.owner_commands import CommandContext
from app.services.prepaid_funding_reconstruction import (
    LEGACY_FINANCIAL_HANDOFF_AT,
    prepaid_funding_incomplete_source_account_ids,
    prepaid_funding_opening_source_incomplete_account_ids,
    verified_prepaid_funding_balance,
    verified_prepaid_funding_balances,
)
from app.services.prepaid_service_renewals import (
    ExecuteReviewedPrepaidServiceRenewalCommand,
    execute_reviewed_prepaid_service_renewal,
    preview_prepaid_service_renewal,
)
from tests.prepaid_funding_helpers import (
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)
from tests.test_account_credit_deposits import (
    _intent,
    _provider,
    _settle,
    _transaction,
)


def _context(actor: str, key: str) -> CommandContext:
    return CommandContext.system(
        actor=actor,
        scope="customer-subledger-opening:test",
        reason="pytest reviewed opening-position migration",
        idempotency_key=key,
    )


def _candidate(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.min_balance = Decimal("100.00")
    account.splynx_customer_id = None
    account.deposit = None
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    ensure_test_prepaid_contract(db, subscription)
    db.commit()


def _preview(db, *, cutoff: datetime, key: str):
    return record_phase3_opening_preview(
        db,
        RecordPhase3OpeningPreviewCommand(
            cutoff_at=cutoff,
            code_version="pytest-opening",
            database_schema_version="457",
        ),
        context=_context("operator:pytest", key),
    )


def _post_cutover_preview(
    db,
    *,
    account_id: UUID,
    key: str,  # noqa: ANN001
) -> Phase3OpeningPreviewResult:
    return record_post_cutover_account_opening_preview(
        db,
        RecordPostCutoverAccountOpeningPreviewCommand(
            account_id=account_id,
            code_version="pytest-post-cutover-opening",
            database_schema_version="471",
        ),
        context=_context("operator:pytest", key),
    )


def _migrated_opening_preview(
    db,
    *,
    account_id: UUID,
    position_at: datetime,
    legacy_position: Decimal,
    key: str,
):
    return record_post_cutover_migrated_account_opening_preview(
        db,
        RecordPostCutoverMigratedAccountOpeningPreviewCommand(
            account_id=account_id,
            source=ReviewedMigratedOpeningSource(
                position_at=position_at,
                legacy_position=legacy_position,
                evidence_ref="finance-review:pytest-migrated-opening",
                evidence_sha256="a" * 64,
            ),
            code_version="pytest-migrated-opening",
            database_schema_version="pytest-head",
        ),
        context=_context("operator:pytest", key),
    )


def _approve(db, run_id, *, at: datetime) -> None:
    BillingShadowVerification.approve_operator(
        db,
        run_id=run_id,
        approved_at=at,
        context=_context("operator:pytest", f"opening-operator:{run_id}"),
    )
    BillingShadowVerification.approve_finance(
        db,
        run_id=run_id,
        approved_at=at,
        context=_context("finance:pytest", f"opening-finance:{run_id}"),
    )


def _capture(db, preview, *, key: str):
    return capture_customer_subledger_opening_positions(
        db,
        CaptureCustomerSubledgerOpeningsCommand(
            context=_context("operator:pytest", key),
            verification_run_id=preview.run_id,
            expected_result_fingerprint=preview.result_fingerprint,
            review_reference="pytest:finance-reviewed-opening-run",
        ),
    )


def test_single_account_opening_preview_requires_active_authority(
    db_session, subscriber_account, subscription
):
    account_id = subscriber_account.id
    _candidate(db_session, subscriber_account, subscription)

    with pytest.raises(BillingShadowVerificationError) as exc:
        _post_cutover_preview(
            db_session,
            account_id=account_id,
            key="opening-preview-before-authority",
        )

    assert exc.value.code.endswith("post_cutover_scope_unavailable")


def test_approved_residual_closes_position_without_double_counting_forward_fact(
    db_session, subscriber_account, subscription, monkeypatch
):
    _candidate(db_session, subscriber_account, subscription)
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber_account.id,
        Decimal("100.00"),
        position_at=datetime(2026, 3, 16, tzinfo=UTC),
    )

    provider = _provider(db_session)
    intent = _intent(db_session, subscriber_account, provider, amount="1000.00")
    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(
            intent,
            amount="1007.00",
            provider_fee="7.00",
            external_id="opening-forward-deposit",
        ),
    )
    db_session.commit()

    settlement_aggregate = (
        customer_financial_ledger.native_customer_financial_balances_by_currency
    )

    def legacy_gross_aggregate(db, account_ids, *, after):  # noqa: ANN001
        """Reproduce the exact pre-v7.98.3 gross-payment interpretation."""

        balances = settlement_aggregate(db, account_ids, after=after)
        assert set(account_ids) == {subscriber_account.id}
        balances[subscriber_account.id]["NGN"] += Decimal("7.00")
        return balances

    monkeypatch.setattr(
        customer_financial_ledger,
        "native_customer_financial_balances_by_currency",
        legacy_gross_aggregate,
    )
    cutoff = datetime.now(UTC)

    preview = _preview(db_session, cutoff=cutoff, key="opening-preview-1")
    assert preview.capture_eligible_count == 1
    assert preview.quarantined_count == 0
    db_session.commit()
    _approve(db_session, preview.run_id, at=cutoff)

    result = _capture(db_session, preview, key="opening-capture-1")

    assert result.captured_count == 1
    assert result.positive_total == Decimal("107.00")
    opening = db_session.query(CustomerSubledgerOpeningPosition).one()
    assert Decimal(opening.legacy_position) == Decimal("1107.00")
    assert Decimal(opening.shadow_position_before) == Decimal("1000.00")
    assert Decimal(opening.opening_delta) == Decimal("107.00")
    group = (
        db_session.query(CustomerPostingGroup)
        .filter(
            CustomerPostingGroup.command_kind == PostingCommandKind.opening_position
        )
        .one()
    )
    assert group.source_kind == "customer_subledger_opening_position"
    assert group.source_id == opening.id
    assert len(group.effects) == 1
    assert group.effects[0].effect is PositionEffectKind.customer_credit_created
    assert Decimal(group.effects[0].amount) == Decimal("107.00")

    # Captured opening evidence is inert until the separate authority cutover.
    # Current settlement semantics therefore still advance from the older
    # reconstruction baseline at this point.
    monkeypatch.undo()
    assert verified_prepaid_funding_balance(
        db_session, subscriber_account.id
    ) == Decimal("1100.00")
    monkeypatch.setattr(
        customer_financial_ledger,
        "native_customer_financial_balances_by_currency",
        legacy_gross_aggregate,
    )

    legacy = verified_prepaid_funding_balance(db_session, subscriber_account.id)
    shadow = resolve_position(
        db_session,
        account_id=subscriber_account.id,
        currency="NGN",
        authority=BillingRecordAuthority.shadow,
    )
    assert shadow.unapplied_customer_credit + shadow.prepaid_funding_reserved == legacy

    db_session.commit()
    parity = record_phase3_subledger_parity(
        db_session,
        RecordPhase3SubledgerParityCommand(
            cutoff_at=cutoff,
            observation_started_at=cutoff - timedelta(days=1),
            observation_ended_at=cutoff,
            code_version="pytest-opening",
            database_schema_version="457",
        ),
        context=_context("operator:pytest", "opening-parity-1"),
    )
    assert parity.parity_count == 1
    assert parity.variance_count == 0
    assert parity.unwrapped_fact_count == 0
    assert parity.blocker_count == 0

    db_session.commit()
    _approve(db_session, parity.run_id, at=cutoff)
    cutover = activate_customer_subledger_authority(
        db_session,
        ActivateCustomerSubledgerAuthorityCommand(
            context=_context("operator:pytest", "subledger-cutover-1"),
            verification_run_id=parity.run_id,
            expected_result_fingerprint=parity.result_fingerprint,
            review_reference="pytest:approved-subledger-parity",
        ),
    )
    assert cutover.replayed is False

    monkeypatch.undo()
    authoritative_intent = _intent(
        db_session, subscriber_account, provider, amount="2000.00"
    )
    _settle(
        db_session,
        intent_id=authoritative_intent.id,
        transaction=_transaction(
            authoritative_intent,
            amount="2011.00",
            provider_fee="11.00",
            external_id="opening-post-cutover-deposit",
        ),
    )
    authoritative_group = (
        db_session.query(CustomerPostingGroup)
        .filter(
            CustomerPostingGroup.producer_owner == "financial.account_credit_deposits",
            CustomerPostingGroup.authority == BillingRecordAuthority.authoritative,
        )
        .one()
    )
    assert authoritative_group.authority is BillingRecordAuthority.authoritative
    default_position = resolve_position(
        db_session,
        account_id=subscriber_account.id,
        currency="NGN",
    )
    assert default_position.authority is BillingRecordAuthority.authoritative
    assert verified_prepaid_funding_balances(db_session, [subscriber_account.id]) == {
        subscriber_account.id: Decimal("3107.00")
    }
    assert (
        default_position.unapplied_customer_credit
        + default_position.prepaid_funding_reserved
        == verified_prepaid_funding_balance(db_session, subscriber_account.id)
    )

    db_session.commit()
    replay = _capture(db_session, preview, key="opening-capture-1")
    assert replay.replayed is True
    assert replay.captured_count == 1
    assert (
        db_session.query(CustomerPostingGroup)
        .filter(
            CustomerPostingGroup.command_kind == PostingCommandKind.opening_position
        )
        .count()
        == 1
    )

    # Post-cutover completion is explicitly account-scoped. An unrelated
    # source-incomplete account continues to block the complete-cohort path but
    # cannot prevent an independently complete native account from being reviewed.
    original_opening_id = opening.id
    second_account = Subscriber(
        first_name="Native",
        last_name="Opening",
        email="native-opening-completion@example.com",
        billing_mode=BillingMode.prepaid,
        reseller_id=subscriber_account.reseller_id,
        created_at=LEGACY_FINANCIAL_HANDOFF_AT + timedelta(days=1),
    )
    db_session.add(second_account)
    db_session.flush()
    second_account_id = second_account.id
    second_subscription = Subscription(
        subscriber_id=second_account_id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(second_subscription)
    db_session.flush()
    _candidate(db_session, second_account, second_subscription)
    assert prepaid_funding_incomplete_source_account_ids(
        db_session,
        [second_account_id],
    ) == {second_account_id}
    assert (
        prepaid_funding_opening_source_incomplete_account_ids(
            db_session,
            [second_account_id],
        )
        == set()
    )
    unrelated_blocker = Subscriber(
        first_name="Legacy",
        last_name="Incomplete",
        email="legacy-incomplete-opening@example.com",
        billing_mode=BillingMode.prepaid,
        reseller_id=subscriber_account.reseller_id,
        created_at=LEGACY_FINANCIAL_HANDOFF_AT - timedelta(days=1),
    )
    db_session.add(unrelated_blocker)
    db_session.flush()
    unrelated_blocker_id = unrelated_blocker.id
    blocker_subscription = Subscription(
        subscriber_id=unrelated_blocker_id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(blocker_subscription)
    db_session.flush()
    blocker_subscription_id = blocker_subscription.id
    _candidate(db_session, unrelated_blocker, blocker_subscription)
    assert prepaid_funding_opening_source_incomplete_account_ids(
        db_session,
        [unrelated_blocker_id],
    ) == {unrelated_blocker_id}

    native_account = Subscriber(
        first_name="Native",
        last_name="After Cutover",
        email="native-after-subledger-cutover@example.com",
        billing_mode=BillingMode.prepaid,
        reseller_id=subscriber_account.reseller_id,
        created_at=cutover.cutover_at + timedelta(seconds=1),
    )
    db_session.add(native_account)
    db_session.flush()
    native_account_id = native_account.id
    native_subscription = Subscription(
        subscriber_id=native_account_id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(native_subscription)
    db_session.flush()
    _candidate(db_session, native_account, native_subscription)

    with pytest.raises(BillingShadowVerificationError) as unnecessary_exc:
        _post_cutover_preview(
            db_session,
            account_id=native_account_id,
            key="opening-preview-not-required",
        )
    assert unnecessary_exc.value.code.endswith("opening_not_required")

    with pytest.raises(BillingShadowVerificationError) as cohort_exc:
        _preview(
            db_session,
            cutoff=cutoff + timedelta(minutes=2),
            key="opening-preview-complete-cohort-blocked",
        )
    assert cohort_exc.value.code.endswith("source_cohort_incomplete")

    completion = _post_cutover_preview(
        db_session,
        account_id=second_account_id,
        key="opening-preview-single-account",
    )
    assert completion.cohort_count == 1
    assert completion.capture_eligible_count == 1
    assert completion.quarantined_count == 0
    db_session.commit()
    _approve(
        db_session,
        completion.run_id,
        at=cutover.cutover_at + timedelta(minutes=2),
    )

    second_account.splynx_customer_id = "late-identity-must-invalidate-review"
    db_session.commit()
    with pytest.raises(CustomerSubledgerOpeningError) as stale_exc:
        _capture(
            db_session,
            completion,
            key="opening-capture-single-account",
        )
    assert stale_exc.value.code.endswith("stale_reviewed_preview")
    assert db_session.query(CustomerSubledgerOpeningPosition).count() == 1

    second_account.splynx_customer_id = None
    db_session.commit()
    completed = _capture(
        db_session,
        completion,
        key="opening-capture-single-account",
    )
    assert completed.captured_count == 1
    assert completed.positive_total == Decimal("0.00")
    openings = db_session.query(CustomerSubledgerOpeningPosition).all()
    assert len(openings) == 2
    assert (
        db_session.get(CustomerSubledgerOpeningPosition, original_opening_id) is opening
    )
    native_opening = next(
        row for row in openings if row.account_id == second_account_id
    )
    assert native_opening.baseline_id is None
    assert Decimal(native_opening.legacy_position) == Decimal("0.00")
    assert Decimal(native_opening.opening_delta) == Decimal("0.00")
    assert (
        db_session.query(CustomerSubledgerOpeningPosition)
        .filter(CustomerSubledgerOpeningPosition.account_id == unrelated_blocker_id)
        .count()
        == 0
    )
    assert (
        prepaid_funding_incomplete_source_account_ids(
            db_session,
            [second_account_id],
        )
        == set()
    )
    assert verified_prepaid_funding_balance(
        db_session,
        second_account_id,
    ) == Decimal("0.00")
    assert verified_prepaid_funding_balance(db_session, native_account_id) == Decimal(
        "0.00"
    )

    # A carried-in migrated account uses a separate, finance-evidenced path.
    # The reviewed source amount is the original authority-cutoff position;
    # later canonical facts remain later facts and are not folded into it.
    unrelated_blocker.splynx_customer_id = "16382"
    db_session.add(
        LedgerEntry(
            account_id=unrelated_blocker_id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.other,
            amount=Decimal("8477.75"),
            currency="NGN",
            memo="pytest reviewed post-cutoff credit-note effect",
            effective_date=cutoff + timedelta(days=1),
        )
    )
    db_session.commit()

    with pytest.raises(BillingShadowVerificationError) as cutoff_exc:
        _migrated_opening_preview(
            db_session,
            account_id=unrelated_blocker_id,
            position_at=cutoff + timedelta(seconds=1),
            legacy_position=Decimal("67334.75"),
            key="opening-preview-migrated-wrong-cutoff",
        )
    assert cutoff_exc.value.code.endswith("reviewed_source_cutoff_mismatch")

    migrated = _migrated_opening_preview(
        db_session,
        account_id=unrelated_blocker_id,
        position_at=cutoff,
        legacy_position=Decimal("67334.75"),
        key="opening-preview-migrated-account",
    )
    assert migrated.legacy_position == Decimal("67334.75")
    assert migrated.shadow_position_before == Decimal("0.00")
    assert migrated.opening_delta == Decimal("67334.75")
    assert migrated.source_evidence_sha256 == "a" * 64
    db_session.commit()

    migrated_replay = _migrated_opening_preview(
        db_session,
        account_id=unrelated_blocker_id,
        position_at=cutoff,
        legacy_position=Decimal("67334.75"),
        key="opening-preview-migrated-account",
    )
    assert migrated_replay.replayed is True
    assert migrated_replay.result_fingerprint == migrated.result_fingerprint

    _approve(
        db_session,
        migrated.run_id,
        at=cutover.cutover_at + timedelta(minutes=3),
    )
    unrelated_blocker.splynx_customer_id = "99999"
    db_session.commit()
    with pytest.raises(CustomerSubledgerOpeningError) as migrated_stale_exc:
        _capture(
            db_session,
            migrated,
            key="opening-capture-migrated-account",
        )
    assert migrated_stale_exc.value.code.endswith("stale_reviewed_preview")
    unrelated_blocker.splynx_customer_id = "16382"
    db_session.commit()
    migrated_capture = _capture(
        db_session,
        migrated,
        key="opening-capture-migrated-account",
    )
    assert migrated_capture.captured_count == 1
    assert migrated_capture.positive_total == Decimal("67334.75")
    assert verified_prepaid_funding_balance(
        db_session, unrelated_blocker_id
    ) == Decimal("75812.50")

    period_start = cutoff + timedelta(days=7)
    period_end = cutoff + timedelta(days=38)
    renewal_preview = preview_prepaid_service_renewal(
        db_session,
        subscription_id=blocker_subscription_id,
        starts_at=period_start,
        ends_at=period_end,
        amount=Decimal("18812.50"),
    )
    assert renewal_preview.allowed is True
    assert renewal_preview.funding_before == Decimal("75812.50")
    db_session.commit()
    reviewed_renewal = execute_reviewed_prepaid_service_renewal(
        db_session,
        ExecuteReviewedPrepaidServiceRenewalCommand(
            context=_context("operator:pytest", "execute-reviewed-missed-renewal"),
            subscription_id=blocker_subscription_id,
            starts_at=period_start,
            ends_at=period_end,
            amount=Decimal("18812.50"),
            currency="NGN",
            expected_preview_fingerprint=renewal_preview.fingerprint,
            evidence_ref="finance-review:pytest-migrated-opening",
        ),
    )
    assert reviewed_renewal.renewal.preview.funding_after == Decimal("57000.00")
    assert reviewed_renewal.outcome is not None
    refreshed_subscription = db_session.get(Subscription, blocker_subscription_id)
    assert refreshed_subscription is not None
    assert refreshed_subscription.next_billing_at is not None
    stored_next_billing = refreshed_subscription.next_billing_at
    if stored_next_billing.tzinfo is None:
        stored_next_billing = stored_next_billing.replace(tzinfo=UTC)
    assert stored_next_billing == period_end
    assert verified_prepaid_funding_balance(
        db_session, unrelated_blocker_id
    ) == Decimal("57000.00")


def test_capture_requires_both_immutable_approvals(
    db_session, subscriber_account, subscription
):
    _candidate(db_session, subscriber_account, subscription)
    materialize_test_prepaid_opening_balance(
        db_session, subscriber_account.id, Decimal("25.00")
    )
    preview = _preview(
        db_session,
        cutoff=datetime(2026, 8, 2, 12, tzinfo=UTC),
        key="opening-preview-unapproved",
    )

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        _capture(db_session, preview, key="opening-capture-unapproved")

    assert exc.value.code.endswith("approval_required")
    assert db_session.query(CustomerSubledgerOpeningPosition).count() == 0


def test_posting_failure_rolls_back_every_opening_row(
    db_session, subscriber_account, subscription, monkeypatch
):
    _candidate(db_session, subscriber_account, subscription)
    materialize_test_prepaid_opening_balance(
        db_session, subscriber_account.id, Decimal("25.00")
    )
    cutoff = datetime(2026, 8, 2, 12, tzinfo=UTC)
    preview = _preview(
        db_session,
        cutoff=cutoff,
        key="opening-preview-atomicity",
    )
    db_session.commit()
    _approve(db_session, preview.run_id, at=cutoff)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("opening posting unavailable")

    monkeypatch.setattr(
        "app.services.billing.subledger_opening.stage_posting_group", _boom
    )
    with pytest.raises(RuntimeError, match="opening posting unavailable"):
        _capture(db_session, preview, key="opening-capture-atomicity")

    assert db_session.query(CustomerSubledgerOpeningPosition).count() == 0
    assert (
        db_session.query(CustomerPostingGroup)
        .filter(
            CustomerPostingGroup.command_kind == PostingCommandKind.opening_position
        )
        .count()
        == 0
    )


def test_unwrapped_money_after_preview_is_not_hidden_by_opening_capture(
    db_session, subscriber_account, subscription
):
    _candidate(db_session, subscriber_account, subscription)
    materialize_test_prepaid_opening_balance(
        db_session, subscriber_account.id, Decimal("100.00")
    )
    cutoff = datetime(2026, 8, 2, 12, tzinfo=UTC)
    preview = _preview(db_session, cutoff=cutoff, key="opening-preview-stale-money")
    db_session.commit()
    _approve(db_session, preview.run_id, at=cutoff)

    # Simulate a producer that changed the legacy position after the reviewed
    # snapshot without staging a posting. Capture remains bound to the old
    # evidence and therefore cannot absorb or conceal the new gap.
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.other,
            amount=Decimal("10.00"),
            currency="NGN",
            memo="pytest unwrapped post-preview money fact",
            effective_date=cutoff + timedelta(seconds=1),
        )
    )
    db_session.commit()
    _capture(db_session, preview, key="opening-capture-stale-money")
    db_session.commit()

    parity = record_phase3_subledger_parity(
        db_session,
        RecordPhase3SubledgerParityCommand(
            cutoff_at=cutoff + timedelta(seconds=2),
            observation_started_at=cutoff,
            observation_ended_at=cutoff + timedelta(seconds=2),
            code_version="pytest-opening",
            database_schema_version="457",
        ),
        context=_context("operator:pytest", "opening-parity-stale-money"),
    )

    assert parity.variance_count == 1
    assert parity.blocker_count == 1
    with pytest.raises(CustomerSubledgerOpeningError):
        activate_customer_subledger_authority(
            db_session,
            ActivateCustomerSubledgerAuthorityCommand(
                context=_context("operator:pytest", "cutover-stale-money"),
                verification_run_id=parity.run_id,
                expected_result_fingerprint=parity.result_fingerprint,
                review_reference="pytest:must-not-activate-stale-money",
            ),
        )
