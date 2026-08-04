"""Reviewed prepaid reconstruction is the final runtime funding authority."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.catalog import BillingMode
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from app.services import customer_financial_ledger, prepaid_funding_attestation
from app.services.customer_financial_position import (
    prepaid_available_balance,
    prepaid_available_balances,
)
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
    PrepaidFundingReconstructionError,
    apply_prepaid_funding_reconstruction,
    authority_cutover_batch,
    parse_reconstruction_manifest,
    preview_prepaid_funding_reconstruction,
)
from tests.prepaid_funding_test_support import (
    sealed_reconstruction_payload,
    sign_test_reconstruction_manifest,
    trust_test_reconstruction_signer,
)


@pytest.fixture(autouse=True)
def _trust_reconstruction_signer(monkeypatch):  # noqa: ANN001
    trust_test_reconstruction_signer(monkeypatch)


@pytest.fixture(autouse=True)
def _remove_native_install_bootstrap(db_session):  # noqa: ANN001
    db_session.query(PrepaidFundingReconstructionBatch).filter(
        PrepaidFundingReconstructionBatch.source
        == "pytest-empty-native-install-cutover"
    ).delete(synchronize_session=False)
    db_session.commit()


def _payload(position_at: datetime, balances: dict[object, str]) -> dict:
    return sealed_reconstruction_payload(position_at, balances)


def _apply(db, position_at: datetime, balances: dict[object, str]):
    payload = _payload(position_at, balances)
    digest = parse_reconstruction_manifest(payload["manifest"]).manifest_sha256
    return apply_prepaid_funding_reconstruction(
        db,
        payload,
        expected_manifest_sha256=digest,
        evidence_ref="finance-review:prepaid-reconstruction-test",
        approved_by="billing-operations-test",
        expected_account_ids=set(balances),
        now=position_at + timedelta(minutes=1),
    )


def test_preview_requires_the_exact_nonempty_cohort(db_session, subscriber):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})

    empty = preview_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_account_ids=set(),
        now=position_at + timedelta(minutes=1),
    )
    assert "reconstruction_expected_cohort_empty" in empty.blockers
    assert any(
        blocker.startswith("unexpected_reconstruction_account:")
        for blocker in empty.blockers
    )

    missing = preview_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_account_ids={subscriber.id, uuid.uuid4()},
        now=position_at + timedelta(minutes=1),
    )
    assert any(
        blocker.startswith("missing_reconstruction_account:")
        for blocker in missing.blockers
    )


def test_apply_rejects_candidate_added_after_manifest_was_sealed(
    db_session, subscriber
):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})
    digest = parse_reconstruction_manifest(payload["manifest"]).manifest_sha256
    account_added_during_review = uuid.uuid4()

    with pytest.raises(
        PrepaidFundingReconstructionError,
        match="missing_reconstruction_account",
    ):
        apply_prepaid_funding_reconstruction(
            db_session,
            payload,
            expected_manifest_sha256=digest,
            evidence_ref="finance-review:stale-cohort-test",
            approved_by="billing-operations-test",
            expected_account_ids={subscriber.id, account_added_during_review},
            now=position_at + timedelta(minutes=1),
        )

    assert authority_cutover_batch(db_session) is None


def test_apply_is_hash_bound_idempotent_and_marks_one_final_cutover(
    db_session, subscriber
):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})
    digest = parse_reconstruction_manifest(payload["manifest"]).manifest_sha256

    with pytest.raises(PrepaidFundingReconstructionError, match="reviewed hash"):
        apply_prepaid_funding_reconstruction(
            db_session,
            payload,
            expected_manifest_sha256="0" * 64,
            evidence_ref="finance-review:wrong-hash",
            approved_by="billing-operations-test",
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )

    first = _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.commit()
    replay = apply_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_manifest_sha256=digest,
        evidence_ref="finance-review:prepaid-reconstruction-test",
        approved_by="billing-operations-test",
        expected_account_ids={subscriber.id},
        now=position_at + timedelta(minutes=2),
    )

    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.batch.id == first.batch.id
    assert authority_cutover_batch(db_session).id == first.batch.id
    assert first.batch.is_authority_cutover is True
    assert first.batch.attestation_sha256 == first.preview.attestation.envelope_sha256
    assert (
        first.batch.manifest_payload_sha256
        == first.preview.attestation.manifest_payload_sha256
    )
    assert (
        first.batch.attestation_key_fingerprint_sha256
        == first.preview.attestation.key_fingerprint_sha256
    )
    assert (
        first.batch.blocker_manifest_sha256
        == first.preview.manifest.blocker_manifest_sha256
    )
    assert (
        first.batch.candidate_cohort_sha256
        == first.preview.manifest.candidate_cohort_sha256
    )


def test_unsigned_or_tampered_reconstruction_cannot_reach_preview(
    db_session, subscriber
):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})

    with pytest.raises(ValueError, match="sealed reconstruction manifest"):
        preview_prepaid_funding_reconstruction(
            db_session,
            payload["manifest"],
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )

    tampered = copy.deepcopy(payload)
    tampered["manifest"]["accounts"][0]["available_balance"] = "999.00"
    with pytest.raises(ValueError, match="does not match the manifest content"):
        preview_prepaid_funding_reconstruction(
            db_session,
            tampered,
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )


def test_reconstruction_rejects_a_signer_outside_configured_trust(
    db_session, subscriber
):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    trusted_payload = _payload(position_at, {subscriber.id: "100.00"})
    other_key = Ed25519PrivateKey.generate()
    other_private_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    untrusted_payload = prepaid_funding_attestation.sign_prepaid_funding_manifest(
        trusted_payload["manifest"],
        private_key_pem=other_private_pem,
        signed_at=position_at + timedelta(seconds=2),
    )

    with pytest.raises(ValueError, match="not the configured trust key"):
        preview_prepaid_funding_reconstruction(
            db_session,
            untrusted_payload,
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )


def test_signed_manifest_rejects_materialized_and_quarantined_overlap(
    db_session, subscriber
):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})
    manifest = copy.deepcopy(payload["manifest"])
    manifest["blocker_count"] = 1
    manifest["blocker_manifest"]["blockers"] = [
        {"account_id": str(subscriber.id), "reason": "missing_source_baseline"}
    ]
    manifest["blocker_manifest_sha256"] = (
        prepaid_funding_attestation.canonical_payload_sha256(
            manifest["blocker_manifest"]
        )
    )
    sealed = sign_test_reconstruction_manifest(
        manifest,
        signed_at=position_at + timedelta(seconds=2),
    )

    with pytest.raises(ValueError, match="both materialized and quarantined"):
        preview_prepaid_funding_reconstruction(
            db_session,
            sealed,
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )


def test_signed_manifest_rejects_a_partial_source_cohort(db_session, subscriber):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    quarantined = Subscriber(
        first_name="Quarantined",
        last_name="Funding",
        email="quarantined-prepaid-funding@example.com",
        billing_mode=BillingMode.prepaid,
        reseller_id=subscriber.reseller_id,
        created_at=position_at - timedelta(days=1),
    )
    db_session.add(quarantined)
    db_session.commit()
    payload = sealed_reconstruction_payload(
        position_at,
        {subscriber.id: "100.00"},
        quarantined={quarantined.id: "plan_decision_not_replayed"},
    )

    preview = preview_prepaid_funding_reconstruction(
        db_session,
        payload,
        expected_account_ids={subscriber.id, quarantined.id},
        now=position_at + timedelta(minutes=1),
    )

    assert preview.ready is False
    assert preview.create_count == 1
    assert preview.manifest.quarantined_account_ids == (quarantined.id,)
    assert "reconstruction_source_cohort_incomplete" in preview.blockers
    with pytest.raises(
        PrepaidFundingReconstructionError,
        match="reconstruction_source_cohort_incomplete",
    ):
        apply_prepaid_funding_reconstruction(
            db_session,
            payload,
            expected_manifest_sha256=preview.manifest.manifest_sha256,
            evidence_ref="finance-review:partial-source-must-fail",
            approved_by="billing-operations-test",
            expected_account_ids={subscriber.id, quarantined.id},
            now=position_at + timedelta(minutes=1),
        )

    assert authority_cutover_batch(db_session) is None


def test_existing_semantic_manifest_cannot_be_resealed_silently(db_session, subscriber):
    position_at = datetime.now(UTC) - timedelta(minutes=5)
    payload = _payload(position_at, {subscriber.id: "100.00"})
    first = _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.commit()
    resealed = sign_test_reconstruction_manifest(
        payload["manifest"],
        signed_at=position_at + timedelta(seconds=3),
    )

    preview = preview_prepaid_funding_reconstruction(
        db_session,
        resealed,
        expected_account_ids={subscriber.id},
        now=position_at + timedelta(minutes=1),
    )
    assert preview.manifest.manifest_sha256 == first.batch.manifest_sha256
    assert "reconstruction_existing_attestation_mismatch" in preview.blockers

    with pytest.raises(PrepaidFundingReconstructionError, match="attestation_mismatch"):
        apply_prepaid_funding_reconstruction(
            db_session,
            resealed,
            expected_manifest_sha256=first.batch.manifest_sha256,
            evidence_ref="finance-review:prepaid-reconstruction-test",
            approved_by="billing-operations-test",
            expected_account_ids={subscriber.id},
            now=position_at + timedelta(minutes=1),
        )


def test_runtime_uses_baseline_plus_native_events_and_never_splynx(
    db_session, subscriber
):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime.now(UTC) - timedelta(days=1)
    _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.add_all(
        [
            SplynxBillingTransaction(
                splynx_transaction_id=991,
                splynx_customer_id=1001,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("9999.00"),
                description="Legacy mirror must not return",
                transaction_date=date.today(),
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                refunded_amount=Decimal("0.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=position_at + timedelta(hours=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="Approved post-reconstruction debit",
                effective_date=position_at + timedelta(hours=2),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("777.00"),
                currency="NGN",
                memo="Approved pre-reconstruction credit",
                effective_date=position_at - timedelta(hours=1),
                created_at=position_at - timedelta(hours=1),
            ),
        ]
    )
    db_session.commit()

    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("115.00")


def test_runtime_batches_accounts_that_share_a_reviewed_position(
    db_session, subscriber, monkeypatch
):
    subscriber.billing_mode = BillingMode.prepaid
    second = Subscriber(
        first_name="Second",
        last_name="Prepaid",
        email="second-prepaid-reconstruction@example.com",
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(second)
    db_session.flush()
    position_at = datetime.now(UTC) - timedelta(days=1)
    _apply(
        db_session,
        position_at,
        {subscriber.id: "100.00", second.id: "50.00"},
    )
    db_session.commit()

    calls: list[tuple[frozenset[object], datetime]] = []
    aggregate = customer_financial_ledger.native_customer_financial_balances_by_currency

    def tracked_aggregate(db, account_ids, *, after):  # noqa: ANN001
        calls.append((frozenset(account_ids), after))
        return aggregate(db, account_ids, after=after)

    monkeypatch.setattr(
        customer_financial_ledger,
        "native_customer_financial_balances_by_currency",
        tracked_aggregate,
    )

    assert prepaid_available_balances(db_session, [subscriber.id, second.id]) == {
        subscriber.id: Decimal("100.00"),
        second.id: Decimal("50.00"),
    }
    assert calls == [
        (frozenset({subscriber.id, second.id}), position_at),
    ]


def test_reviewed_supersession_is_append_only_not_legacy_rollback(
    db_session, subscriber
):
    first_at = datetime.now(UTC) - timedelta(days=2)
    first = _apply(db_session, first_at, {subscriber.id: "100.00"})
    db_session.commit()
    second = _apply(
        db_session,
        first_at + timedelta(days=1),
        {subscriber.id: "125.00"},
    )
    db_session.commit()

    baselines = db_session.query(PrepaidFundingBaseline).all()
    inactive = [row for row in baselines if not row.is_active]
    active = [row for row in baselines if row.is_active]
    assert len(inactive) == 1
    assert len(active) == 1
    assert inactive[0].superseded_at is not None
    assert first.batch.is_authority_cutover is True
    assert second.batch.is_authority_cutover is False
    assert authority_cutover_batch(db_session).id == first.batch.id
    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("125.00")


def test_pre_cutover_account_without_baseline_fails_closed(db_session, subscriber):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime.now(UTC) - timedelta(minutes=1)
    subscriber.created_at = position_at - timedelta(days=1)
    missing = Subscriber(
        first_name="Missing",
        last_name="Baseline",
        email="missing-prepaid-baseline@example.com",
        billing_mode=BillingMode.prepaid,
        created_at=position_at - timedelta(days=1),
    )
    db_session.add(missing)
    db_session.commit()
    _apply(
        db_session,
        position_at,
        {subscriber.id: "100.00"},
    )
    db_session.commit()

    with pytest.raises(PrepaidFundingBaselineMissingError, match="baseline missing"):
        prepaid_available_balance(db_session, missing.id)


def test_post_cutover_native_account_starts_from_zero(db_session, subscriber):
    subscriber.billing_mode = BillingMode.prepaid
    position_at = datetime.now(UTC) - timedelta(days=1)
    _apply(db_session, position_at, {subscriber.id: "100.00"})
    db_session.commit()

    native = Subscriber(
        first_name="Post",
        last_name="Cutover",
        email="post-cutover-prepaid@example.com",
        billing_mode=BillingMode.prepaid,
        created_at=position_at + timedelta(hours=1),
    )
    db_session.add(native)
    db_session.flush()
    db_session.add(
        Payment(
            account_id=native.id,
            amount=Decimal("50.00"),
            refunded_amount=Decimal("0.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=position_at + timedelta(hours=2),
        )
    )
    db_session.commit()

    assert prepaid_available_balance(db_session, native.id) == Decimal("50.00")
