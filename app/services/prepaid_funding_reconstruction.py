"""Owner of reviewed prepaid funding reconstruction and authority cutover.

Reconstruction materializes an independently proven customer position at one
timestamp. Runtime funding is that baseline plus canonical native events after
the timestamp. Source exports and bank statements are evidence inputs only;
they are never queried as runtime balances.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.customer_subledger import (
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningPosition,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.subscriber import Subscriber
from app.services import display_format
from app.services.common import coerce_uuid, round_money
from app.services.prepaid_funding_attestation import (
    RECONSTRUCTION_MANIFEST_SCHEMA,
    VerifiedPrepaidFundingAttestation,
    candidate_cohort_sha256,
    verify_prepaid_funding_manifest,
)


class PrepaidFundingReconstructionError(ValueError):
    pass


class PrepaidFundingBaselineMissingError(RuntimeError):
    pass


LEGACY_FINANCIAL_HANDOFF_AT = datetime(2026, 6, 18, tzinfo=UTC)


class PrepaidOpeningTargetOrigin(StrEnum):
    """Authoritative provenance for one opening-position preview target."""

    reviewed_reconstruction = "reviewed_reconstruction"
    reviewed_subledger_opening = "reviewed_subledger_opening"
    finance_reviewed_migrated_history = "finance_reviewed_migrated_history"
    native_after_handoff = "native_after_handoff"


@dataclass(frozen=True, slots=True)
class PrepaidOpeningTarget:
    """Typed source target consumed only by reviewed opening verification."""

    account_id: UUID
    currency: str
    amount: Decimal
    origin: PrepaidOpeningTargetOrigin
    source_position_at: datetime


@dataclass(frozen=True)
class ReconstructionRow:
    account_id: UUID
    amount: Decimal


@dataclass(frozen=True)
class ReconstructionManifest:
    source: str
    position_at: datetime
    currency: str
    rows: tuple[ReconstructionRow, ...]
    quarantined_account_ids: tuple[UUID, ...]
    candidate_cohort_sha256: str
    blocker_manifest_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class ReconstructionPreview:
    manifest: ReconstructionManifest
    attestation: VerifiedPrepaidFundingAttestation
    blockers: tuple[str, ...]
    create_count: int
    replace_count: int
    unchanged_count: int
    total_amount: Decimal
    idempotent_replay: bool

    @property
    def ready(self) -> bool:
        return not self.blockers

    def report(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "source": self.manifest.source,
            "position_at": self.manifest.position_at.isoformat(),
            "currency": self.manifest.currency,
            "manifest_sha256": self.manifest.manifest_sha256,
            "manifest_payload_sha256": self.attestation.manifest_payload_sha256,
            "attestation_sha256": self.attestation.envelope_sha256,
            "attestation_key_fingerprint_sha256": (
                self.attestation.key_fingerprint_sha256
            ),
            "attestation_signed_at": self.attestation.signed_at.isoformat(),
            "blocker_manifest_sha256": self.manifest.blocker_manifest_sha256,
            "candidate_cohort_sha256": self.manifest.candidate_cohort_sha256,
            "account_count": len(self.manifest.rows),
            "quarantined_account_count": len(self.manifest.quarantined_account_ids),
            "total_amount": f"{self.total_amount:.2f}",
            "create_count": self.create_count,
            "replace_count": self.replace_count,
            "unchanged_count": self.unchanged_count,
            "idempotent_replay": self.idempotent_replay,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class ReconstructionApplyResult:
    batch: PrepaidFundingReconstructionBatch
    preview: ReconstructionPreview
    idempotent_replay: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PrepaidFundingReconstructionError(
            "reconstruction position_at must include a timezone"
        )
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _currency(value: object) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise PrepaidFundingReconstructionError(
            "reconstruction currency must be a three-letter code"
        )
    return currency


def default_prepaid_funding_currency(db: Session) -> str:
    return _currency(display_format.default_currency(db))


def _money(value: object) -> Decimal:
    try:
        return round_money(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PrepaidFundingReconstructionError(
            f"invalid reconstructed amount: {value!s}"
        ) from exc


def _parse_position_at(payload: dict[str, Any]) -> datetime:
    raw = payload.get("position_at", payload.get("captured_at"))
    if not isinstance(raw, str) or not raw.strip():
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest requires position_at or captured_at"
        )
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrepaidFundingReconstructionError(
            "reconstruction position_at is invalid"
        ) from exc
    return _as_utc(parsed)


def _normalized_manifest_payload(
    *,
    source: str,
    position_at: datetime,
    currency: str,
    rows: tuple[ReconstructionRow, ...],
    candidate_count: int,
    blocker_count: int,
    candidate_hash: str,
    blocker_hash: str,
) -> dict[str, Any]:
    return {
        "schema": RECONSTRUCTION_MANIFEST_SCHEMA,
        "source": source,
        "position_at": position_at.isoformat().replace("+00:00", "Z"),
        "currency": currency,
        "candidate_accounts": candidate_count,
        "candidate_cohort_sha256": candidate_hash,
        "blocker_manifest_sha256": blocker_hash,
        "blocker_count": blocker_count,
        "accounts": [
            {
                "account_id": str(row.account_id),
                "available_balance": f"{row.amount:.2f}",
            }
            for row in rows
        ],
    }


def parse_reconstruction_manifest(payload: dict[str, Any]) -> ReconstructionManifest:
    expected_fields = {
        "schema",
        "source",
        "captured_at",
        "currency",
        "candidate_accounts",
        "candidate_cohort_sha256",
        "blocker_manifest_sha256",
        "blocker_count",
        "blocker_manifest",
        "accounts",
    }
    if set(payload) != expected_fields:
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest fields are incomplete or unexpected"
        )
    if payload.get("schema") != RECONSTRUCTION_MANIFEST_SCHEMA:
        raise PrepaidFundingReconstructionError(
            "unsupported prepaid reconstruction manifest schema"
        )
    source = str(payload.get("source") or "").strip()
    if not source:
        raise PrepaidFundingReconstructionError(
            "reconstruction source must not be empty"
        )
    position_at = _parse_position_at(payload)
    currency = _currency(payload.get("currency"))
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest accounts must not be empty"
        )
    by_account: dict[UUID, ReconstructionRow] = {}
    for item in accounts:
        if not isinstance(item, dict):
            raise PrepaidFundingReconstructionError(
                "reconstruction account rows must be objects"
            )
        if set(item) != {"account_id", "available_balance"}:
            raise PrepaidFundingReconstructionError(
                "reconstruction account fields are incomplete or unexpected"
            )
        account_id = coerce_uuid(item.get("account_id"))
        if account_id in by_account:
            raise PrepaidFundingReconstructionError(
                f"duplicate reconstruction account: {account_id}"
            )
        by_account[account_id] = ReconstructionRow(
            account_id=account_id,
            amount=_money(item.get("available_balance")),
        )
    rows = tuple(sorted(by_account.values(), key=lambda row: str(row.account_id)))
    candidate_count = payload.get("candidate_accounts")
    if type(candidate_count) is not int or candidate_count < len(rows):
        raise PrepaidFundingReconstructionError(
            "reconstruction candidate count cannot be smaller than account rows"
        )
    blocker_count = payload.get("blocker_count")
    if type(blocker_count) is not int or blocker_count < 0:
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker count must be a non-negative integer"
        )
    candidate_hash = str(payload.get("candidate_cohort_sha256") or "").strip().lower()
    blocker_hash = str(payload.get("blocker_manifest_sha256") or "").strip().lower()
    if len(blocker_hash) != 64 or any(
        character not in "0123456789abcdef" for character in blocker_hash
    ):
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker manifest hash is invalid"
        )
    blocker_manifest = payload.get("blocker_manifest")
    if not isinstance(blocker_manifest, dict):
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest requires its blocker manifest"
        )
    actual_blocker_hash = hashlib.sha256(
        json.dumps(
            blocker_manifest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if actual_blocker_hash != blocker_hash:
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker manifest hash does not match its content"
        )
    expected_blocker_fields = {
        "schema",
        "source",
        "captured_at",
        "financial_handoff_at",
        "currency",
        "candidate_accounts",
        "candidate_cohort_sha256",
        "blockers",
    }
    if set(blocker_manifest) != expected_blocker_fields:
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker manifest fields are incomplete or unexpected"
        )
    if blocker_manifest.get("schema") != "dotmac.prepaid_funding_blockers.v1":
        raise PrepaidFundingReconstructionError(
            "unsupported prepaid reconstruction blocker manifest schema"
        )
    blocker_rows = blocker_manifest.get("blockers")
    if not isinstance(blocker_rows, list):
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker manifest blockers must be a list"
        )
    quarantined: set[UUID] = set()
    seen_blockers: set[tuple[UUID, str]] = set()
    for item in blocker_rows:
        if not isinstance(item, dict) or set(item) != {"account_id", "reason"}:
            raise PrepaidFundingReconstructionError(
                "reconstruction blocker rows are incomplete or unexpected"
            )
        account_id = coerce_uuid(item.get("account_id"))
        reason = str(item.get("reason") or "").strip()
        if not reason or len(reason) > 120:
            raise PrepaidFundingReconstructionError(
                "reconstruction blocker reason is invalid"
            )
        key = (account_id, reason)
        if key in seen_blockers:
            raise PrepaidFundingReconstructionError(
                "reconstruction blocker row is duplicated"
            )
        seen_blockers.add(key)
        quarantined.add(account_id)
    row_ids = {row.account_id for row in rows}
    if row_ids & quarantined:
        raise PrepaidFundingReconstructionError(
            "reconstruction account cannot be both materialized and quarantined"
        )
    if blocker_count != len(quarantined):
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker count does not match quarantined accounts"
        )
    if candidate_count != len(row_ids | quarantined):
        raise PrepaidFundingReconstructionError(
            "reconstruction candidate count does not match materialized and "
            "quarantined accounts"
        )
    expected_candidate_hash = candidate_cohort_sha256(
        [str(account_id) for account_id in row_ids | quarantined]
    )
    if candidate_hash != expected_candidate_hash:
        raise PrepaidFundingReconstructionError(
            "reconstruction candidate cohort hash does not match materialized "
            "and quarantined accounts"
        )
    if (
        blocker_manifest.get("source") != source
        or blocker_manifest.get("captured_at") != payload.get("captured_at")
        or blocker_manifest.get("currency") != currency
        or blocker_manifest.get("candidate_accounts") != candidate_count
        or blocker_manifest.get("candidate_cohort_sha256") != candidate_hash
    ):
        raise PrepaidFundingReconstructionError(
            "reconstruction blocker manifest does not match the funding cohort"
        )
    try:
        handoff_at = _parse_position_at(
            {"captured_at": blocker_manifest["financial_handoff_at"]}
        )
    except PrepaidFundingReconstructionError as exc:
        raise PrepaidFundingReconstructionError(
            "reconstruction financial handoff timestamp is invalid"
        ) from exc
    if handoff_at > position_at:
        raise PrepaidFundingReconstructionError(
            "reconstruction financial handoff cannot follow the captured position"
        )
    normalized = _normalized_manifest_payload(
        source=source,
        position_at=position_at,
        currency=currency,
        rows=rows,
        candidate_count=candidate_count,
        blocker_count=blocker_count,
        candidate_hash=candidate_hash,
        blocker_hash=blocker_hash,
    )
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReconstructionManifest(
        source=source,
        position_at=position_at,
        currency=currency,
        rows=rows,
        quarantined_account_ids=tuple(sorted(quarantined, key=str)),
        candidate_cohort_sha256=candidate_hash,
        blocker_manifest_sha256=blocker_hash,
        manifest_sha256=digest,
    )


def _active_baselines(
    db: Session, account_ids: set[UUID], currency: str
) -> dict[UUID, PrepaidFundingBaseline]:
    if not account_ids:
        return {}
    return {
        baseline.account_id: baseline
        for baseline in db.scalars(
            select(PrepaidFundingBaseline).where(
                PrepaidFundingBaseline.account_id.in_(account_ids),
                PrepaidFundingBaseline.currency == currency,
                PrepaidFundingBaseline.is_active.is_(True),
            )
        ).all()
    }


def preview_prepaid_funding_reconstruction(
    db: Session,
    payload: dict[str, Any],
    *,
    expected_account_ids: set[object],
    now: datetime | None = None,
) -> ReconstructionPreview:
    manifest_payload, attestation = verify_prepaid_funding_manifest(
        db,
        payload,
        now=now,
    )
    manifest = parse_reconstruction_manifest(manifest_payload)
    row_ids = {row.account_id for row in manifest.rows}
    manifest_account_ids = row_ids | set(manifest.quarantined_account_ids)
    blockers: list[str] = []
    if manifest.quarantined_account_ids:
        blockers.append("reconstruction_source_cohort_incomplete")
    expected = {coerce_uuid(value) for value in expected_account_ids}
    if not expected:
        blockers.append("reconstruction_expected_cohort_empty")
    blockers.extend(
        f"missing_reconstruction_account:{account_id}"
        for account_id in sorted(expected - manifest_account_ids, key=str)
    )
    blockers.extend(
        f"unexpected_reconstruction_account:{account_id}"
        for account_id in sorted(manifest_account_ids - expected, key=str)
    )
    effective_now = _as_utc(now or datetime.now(UTC))
    if manifest.position_at > effective_now:
        blockers.append("reconstruction_position_in_future")
    existing_accounts = set(
        db.scalars(
            select(Subscriber.id).where(Subscriber.id.in_(manifest_account_ids))
        ).all()
    )
    blockers.extend(
        f"reconstruction_account_not_found:{account_id}"
        for account_id in sorted(manifest_account_ids - existing_accounts, key=str)
    )
    active = _active_baselines(db, row_ids, manifest.currency)
    create_count = 0
    replace_count = 0
    unchanged_count = 0
    for row in manifest.rows:
        baseline = active.get(row.account_id)
        if baseline is None:
            create_count += 1
            continue
        baseline_at = _stored_utc(baseline.position_at)
        if baseline_at == manifest.position_at and baseline.amount == row.amount:
            unchanged_count += 1
            continue
        replace_count += 1
        if baseline_at >= manifest.position_at:
            blockers.append(f"reconstruction_position_not_newer:{row.account_id}")
    existing_batch = db.scalar(
        select(PrepaidFundingReconstructionBatch).where(
            PrepaidFundingReconstructionBatch.manifest_sha256
            == manifest.manifest_sha256
        )
    )
    if (
        existing_batch is not None
        and existing_batch.attestation_sha256 != attestation.envelope_sha256
    ):
        blockers.append("reconstruction_existing_attestation_mismatch")
    total = round_money(sum((row.amount for row in manifest.rows), Decimal("0.00")))
    return ReconstructionPreview(
        manifest=manifest,
        attestation=attestation,
        blockers=tuple(blockers),
        create_count=create_count,
        replace_count=replace_count,
        unchanged_count=unchanged_count,
        total_amount=total,
        idempotent_replay=existing_batch is not None,
    )


def apply_prepaid_funding_reconstruction(
    db: Session,
    payload: dict[str, Any],
    *,
    expected_manifest_sha256: str,
    evidence_ref: str,
    approved_by: str,
    expected_account_ids: set[object],
    now: datetime | None = None,
) -> ReconstructionApplyResult:
    evidence = evidence_ref.strip()
    actor = approved_by.strip()
    if not evidence:
        raise PrepaidFundingReconstructionError("evidence_ref is required")
    if not actor:
        raise PrepaidFundingReconstructionError("approved_by is required")
    preview = preview_prepaid_funding_reconstruction(
        db,
        payload,
        expected_account_ids=expected_account_ids,
        now=now,
    )
    if preview.manifest.manifest_sha256 != expected_manifest_sha256.strip().lower():
        raise PrepaidFundingReconstructionError(
            "reconstruction manifest hash does not match reviewed hash"
        )
    existing = db.scalar(
        select(PrepaidFundingReconstructionBatch).where(
            PrepaidFundingReconstructionBatch.manifest_sha256
            == preview.manifest.manifest_sha256
        )
    )
    if preview.blockers:
        raise PrepaidFundingReconstructionError(
            "prepaid funding reconstruction blocked: " + ", ".join(preview.blockers)
        )
    if existing is not None:
        return ReconstructionApplyResult(
            batch=existing,
            preview=preview,
            idempotent_replay=True,
        )

    row_ids = {row.account_id for row in preview.manifest.rows}
    locked_ids = set(
        db.scalars(
            select(Subscriber.id).where(Subscriber.id.in_(row_ids)).with_for_update()
        ).all()
    )
    if locked_ids != row_ids:
        raise PrepaidFundingReconstructionError(
            "reconstruction cohort changed while acquiring account locks"
        )
    applied_at = _as_utc(now or datetime.now(UTC))
    db.execute(
        update(PrepaidFundingBaseline)
        .where(
            PrepaidFundingBaseline.account_id.in_(row_ids),
            PrepaidFundingBaseline.currency == preview.manifest.currency,
            PrepaidFundingBaseline.is_active.is_(True),
        )
        .values(is_active=False, superseded_at=applied_at)
    )
    batch = PrepaidFundingReconstructionBatch(
        manifest_sha256=preview.manifest.manifest_sha256,
        manifest_payload_sha256=preview.attestation.manifest_payload_sha256,
        attestation_sha256=preview.attestation.envelope_sha256,
        attestation_key_fingerprint_sha256=(preview.attestation.key_fingerprint_sha256),
        attestation_signed_at=preview.attestation.signed_at,
        blocker_manifest_sha256=preview.manifest.blocker_manifest_sha256,
        candidate_cohort_sha256=preview.manifest.candidate_cohort_sha256,
        source=preview.manifest.source,
        evidence_ref=evidence,
        position_at=preview.manifest.position_at,
        currency=preview.manifest.currency,
        account_count=len(preview.manifest.rows),
        total_amount=preview.total_amount,
        approved_by=actor,
        is_authority_cutover=(authority_cutover_batch(db) is None),
        approved_at=applied_at,
    )
    db.add(batch)
    db.flush()
    db.add_all(
        [
            PrepaidFundingBaseline(
                batch_id=batch.id,
                account_id=row.account_id,
                currency=preview.manifest.currency,
                amount=row.amount,
                position_at=preview.manifest.position_at,
                is_active=True,
            )
            for row in preview.manifest.rows
        ]
    )
    db.flush()
    return ReconstructionApplyResult(
        batch=batch,
        preview=preview,
        idempotent_replay=False,
    )


def active_prepaid_funding_baseline(
    db: Session, account_id: object, *, currency: str | None = None
) -> PrepaidFundingBaseline | None:
    unit = _currency(currency or default_prepaid_funding_currency(db))
    return db.scalar(
        select(PrepaidFundingBaseline).where(
            PrepaidFundingBaseline.account_id == coerce_uuid(account_id),
            PrepaidFundingBaseline.currency == unit,
            PrepaidFundingBaseline.is_active.is_(True),
        )
    )


def authority_cutover_batch(
    db: Session,
) -> PrepaidFundingReconstructionBatch | None:
    """Return the one irreversible transition away from legacy funding."""
    return db.scalar(
        select(PrepaidFundingReconstructionBatch).where(
            PrepaidFundingReconstructionBatch.is_authority_cutover.is_(True)
        )
    )


def prepaid_funding_incomplete_source_account_ids(
    db: Session,
    account_ids: Iterable[object],
    *,
    currency: str | None = None,
) -> set[UUID]:
    """Return accounts whose one-time history seed is not yet materialized.

    A migrated account without a baseline represents an incomplete all-or-
    nothing source capture, not an unknown customer balance. Complete-history
    preview and parity commands reject the whole cohort while this set is
    non-empty. Native accounts created after the handoff are seeded by the
    complete-history artifact with an explicit zero history component plus
    their canonical native facts.
    """
    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return set()
    cutover = authority_cutover_batch(db)
    if cutover is None:
        return set(ids)
    required_ids = prepaid_funding_opening_required_account_ids(db, ids)
    unit = _currency(currency or default_prepaid_funding_currency(db))
    covered_ids = set(_active_baselines(db, required_ids, unit))
    if db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is not None:
        covered_ids.update(
            coerce_uuid(value)
            for value in db.scalars(
                select(CustomerSubledgerOpeningPosition.account_id).where(
                    CustomerSubledgerOpeningPosition.account_id.in_(required_ids),
                    CustomerSubledgerOpeningPosition.currency == unit,
                )
            ).all()
        )
    return required_ids - covered_ids


def _native_after_handoff_account_ids(
    db: Session,
    account_ids: Iterable[object],
) -> set[UUID]:
    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return set()
    return {
        coerce_uuid(value)
        for value in db.scalars(
            select(Subscriber.id).where(
                Subscriber.id.in_(ids),
                Subscriber.created_at > LEGACY_FINANCIAL_HANDOFF_AT,
                Subscriber.splynx_customer_id.is_(None),
            )
        ).all()
    }


def prepaid_funding_opening_source_incomplete_account_ids(
    db: Session,
    account_ids: Iterable[object],
    *,
    currency: str | None = None,
) -> set[UUID]:
    """Return accounts lacking either reviewed or provably native source evidence.

    This is intentionally narrower than the runtime quarantine resolver. A native
    account created after the fixed legacy handoff has a mathematical zero history
    component and canonical Sub-native facts, so the opening verifier can propose
    its exact current target without consulting Splynx. Runtime money actions remain
    blocked until the approved verifier result is captured as an immutable opening.
    """

    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return set()
    cutover = authority_cutover_batch(db)
    if cutover is None:
        return set(ids)
    required_ids = prepaid_funding_opening_required_account_ids(db, ids)
    unit = _currency(currency or default_prepaid_funding_currency(db))
    covered_ids = set(_active_baselines(db, required_ids, unit))
    if db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is not None:
        covered_ids.update(
            coerce_uuid(value)
            for value in db.scalars(
                select(CustomerSubledgerOpeningPosition.account_id).where(
                    CustomerSubledgerOpeningPosition.account_id.in_(required_ids),
                    CustomerSubledgerOpeningPosition.currency == unit,
                )
            ).all()
        )
    covered_ids.update(_native_after_handoff_account_ids(db, required_ids))
    return required_ids - covered_ids


def preview_prepaid_opening_targets(
    db: Session,
    account_ids: Iterable[object],
    *,
    currency: str | None = None,
    as_of: datetime | None = None,
) -> dict[UUID, PrepaidOpeningTarget]:
    """Resolve typed targets for a fingerprinted opening-position preview.

    Reviewed baselines/openings keep their normal runtime semantics. A missing
    native-after-handoff account is resolved from mathematical zero plus canonical
    native facts only for this review surface; it does not become runtime funding
    authority until the approved opening is captured.
    """

    from app.services.customer_financial_ledger import (
        native_customer_financial_balances_by_currency,
    )

    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return {}
    unit = _currency(currency or default_prepaid_funding_currency(db))
    accounts = {
        account.id: account
        for account in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids))
        ).all()
    }
    unknown = sorted(ids - set(accounts), key=str)
    if unknown:
        raise PrepaidFundingBaselineMissingError(
            "prepaid funding account not found: "
            + ", ".join(str(value) for value in unknown)
        )
    baselines = _active_baselines(db, ids, unit)
    subledger_authority_active = (
        db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is not None
    )
    openings = (
        {
            row.account_id: row
            for row in db.scalars(
                select(CustomerSubledgerOpeningPosition).where(
                    CustomerSubledgerOpeningPosition.account_id.in_(ids),
                    CustomerSubledgerOpeningPosition.currency == unit,
                )
            ).all()
        }
        if subledger_authority_active
        else {}
    )
    native_ids = _native_after_handoff_account_ids(
        db,
        ids - set(baselines) - set(openings),
    )
    unresolved = sorted(ids - set(baselines) - set(openings) - native_ids, key=str)
    if unresolved:
        raise PrepaidFundingBaselineMissingError(
            "verified prepaid funding baseline missing for: "
            + ", ".join(str(value) for value in unresolved)
        )

    targets: dict[UUID, PrepaidOpeningTarget] = {}
    reviewed_ids = ids - native_ids
    if reviewed_ids:
        reviewed = verified_prepaid_funding_balances(
            db,
            reviewed_ids,
            currency=unit,
        )
        for account_id in sorted(reviewed_ids, key=str):
            opening = openings.get(account_id)
            baseline = baselines.get(account_id)
            if opening is not None:
                source_at = _stored_utc(opening.occurred_at)
                origin = PrepaidOpeningTargetOrigin.reviewed_subledger_opening
            else:
                if baseline is None:
                    raise PrepaidFundingBaselineMissingError(
                        f"verified prepaid funding baseline missing for: {account_id}"
                    )
                source_at = _stored_utc(baseline.position_at)
                origin = PrepaidOpeningTargetOrigin.reviewed_reconstruction
            targets[account_id] = PrepaidOpeningTarget(
                account_id=account_id,
                currency=unit,
                amount=round_money(reviewed[account_id]),
                origin=origin,
                source_position_at=source_at,
            )

    if native_ids:
        boundary = _stored_utc(as_of) if as_of is not None else None
        native = (
            native_customer_financial_balances_by_currency(
                db,
                native_ids,
                after=LEGACY_FINANCIAL_HANDOFF_AT,
            )
            if boundary is None
            else native_customer_financial_balances_by_currency(
                db,
                native_ids,
                after=LEGACY_FINANCIAL_HANDOFF_AT,
                before=boundary,
            )
        )
        for account_id in sorted(native_ids, key=str):
            targets[account_id] = PrepaidOpeningTarget(
                account_id=account_id,
                currency=unit,
                amount=round_money(
                    native.get(account_id, {}).get(unit, Decimal("0.00"))
                ),
                origin=PrepaidOpeningTargetOrigin.native_after_handoff,
                source_position_at=LEGACY_FINANCIAL_HANDOFF_AT,
            )
    return targets


def prepaid_funding_opening_required_account_ids(
    db: Session,
    account_ids: Iterable[object],
) -> set[UUID]:
    """Return accounts that existed when subledger authority activated.

    Before activation every requested account requires an opening. After
    activation, accounts created later begin from the authoritative subledger's
    mathematical zero and accumulate only native postings; giving them a
    migration baseline would create a permanent dependency on a retired source.
    Unknown account IDs remain in the required set so callers fail closed.
    """
    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return set()
    cutover_at = db.scalar(
        select(CustomerSubledgerAuthorityCutover.cutover_at).limit(1)
    )
    if cutover_at is None:
        return ids
    boundary = _stored_utc(cutover_at)
    required_account_ids = {
        coerce_uuid(value)
        for value in db.scalars(
            select(Subscriber.id).where(
                Subscriber.id.in_(ids),
                Subscriber.created_at <= boundary,
            )
        ).all()
    }
    known_ids = {
        coerce_uuid(value)
        for value in db.scalars(
            select(Subscriber.id).where(Subscriber.id.in_(ids))
        ).all()
    }
    return required_account_ids | (ids - known_ids)


def prepaid_funding_quarantined_account_ids(
    db: Session,
    account_ids: Iterable[object],
    *,
    currency: str | None = None,
) -> set[UUID]:
    """Compatibility name for the pre-completion enforcement guard."""

    return prepaid_funding_incomplete_source_account_ids(
        db,
        account_ids,
        currency=currency,
    )


def verified_prepaid_funding_balances(
    db: Session,
    account_ids: set[object] | list[object] | tuple[object, ...],
    *,
    currency: str | None = None,
) -> dict[UUID, Decimal]:
    """Resolve the active reviewed opening plus later native events.

    The reconstruction baseline remains active until customer-subledger
    authority is irreversibly activated. After that activation, each captured
    subledger opening's finance-approved ``legacy_position`` supersedes the
    older reconstruction amount and time for verifier reads. This freezes all
    pre-opening financial meaning exactly as reviewed while allowing newer
    native facts to use their current structural semantics.

    Splynx rows are never runtime inputs and no posting is manufactured here.
    """
    from app.services.customer_financial_ledger import (
        native_customer_financial_balances_by_currency,
    )

    unit = _currency(currency or default_prepaid_funding_currency(db))
    ids = {coerce_uuid(value) for value in account_ids}
    if not ids:
        return {}
    baselines = _active_baselines(db, ids, unit)
    cutover = authority_cutover_batch(db)
    if cutover is None:
        raise PrepaidFundingBaselineMissingError(
            "prepaid funding authority cutover has not been materialized"
        )
    subledger_authority_active = (
        db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is not None
    )
    subledger_openings = (
        {
            opening.account_id: opening
            for opening in db.scalars(
                select(CustomerSubledgerOpeningPosition).where(
                    CustomerSubledgerOpeningPosition.account_id.in_(ids),
                    CustomerSubledgerOpeningPosition.currency == unit,
                )
            ).all()
        }
        if subledger_authority_active
        else {}
    )
    accounts = {
        account.id: account
        for account in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids))
        ).all()
    }
    unknown = sorted(ids - set(accounts), key=str)
    if unknown:
        raise PrepaidFundingBaselineMissingError(
            "prepaid funding account not found: "
            + ", ".join(str(value) for value in unknown)
        )
    cutover_position = _stored_utc(cutover.position_at)
    opening_amounts: dict[UUID, Decimal] = {}
    accounts_by_position: dict[datetime, list[UUID]] = {}
    for account_id in sorted(ids, key=str):
        subledger_opening = subledger_openings.get(account_id)
        baseline = baselines.get(account_id)
        if subledger_opening is not None:
            opening_amount = round_money(Decimal(subledger_opening.legacy_position))
            position_at = _stored_utc(subledger_opening.occurred_at)
        elif baseline is None:
            created_at = accounts[account_id].created_at
            account_start = (
                created_at.replace(tzinfo=UTC)
                if created_at.tzinfo is None
                else created_at.astimezone(UTC)
            )
            if account_start <= cutover_position:
                raise PrepaidFundingBaselineMissingError(
                    f"verified prepaid funding baseline missing for: {account_id}"
                )
            opening_amount = Decimal("0.00")
            position_at = cutover_position
        else:
            opening_amount = baseline.amount
            position_at = _stored_utc(baseline.position_at)
        opening_amounts[account_id] = opening_amount
        accounts_by_position.setdefault(position_at, []).append(account_id)

    balances: dict[UUID, Decimal] = {}
    for position_at, positioned_ids in sorted(accounts_by_position.items()):
        native = native_customer_financial_balances_by_currency(
            db,
            positioned_ids,
            after=position_at,
        )
        for account_id in positioned_ids:
            native_delta = native.get(account_id, {}).get(unit, Decimal("0.00"))
            balances[account_id] = round_money(
                opening_amounts[account_id] + native_delta
            )
    return balances


def verified_prepaid_funding_balance(
    db: Session, account_id: object, *, currency: str | None = None
) -> Decimal:
    account_uuid = coerce_uuid(account_id)
    return verified_prepaid_funding_balances(db, [account_uuid], currency=currency)[
        account_uuid
    ]
