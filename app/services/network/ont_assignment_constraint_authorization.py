"""Immutable review evidence for a future ONT assignment constraint cutover.

This owner can request and independently approve or decline authorization
evidence bound to one exact clean coverage snapshot. It deliberately has no DDL
executor and cannot create, validate, enable, disable, or remove a constraint.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ont_assignment_constraint_authorization import (
    OntAssignmentConstraintAuthorizationRequest,
    OntAssignmentConstraintAuthorizationReview,
)
from app.services.audit_helpers import resolve_actor_display_names
from app.services.network.ont_assignment_cutover_coverage import (
    OntAssignmentCutoverCoverageError,
    OntAssignmentCutoverCoverageReport,
    reconcile_ont_assignment_cutover_coverage,
)
from app.services.network.ont_assignment_cutover_verification import (
    OntAssignmentCutoverVerificationError,
    ensure_ont_assignment_cutover_repeatable_snapshot,
)

REVIEW_ACTIONS = frozenset({"approve", "decline"})


class OntAssignmentConstraintAuthorizationError(ValueError):
    """Raised when authorization evidence or confirmation input is invalid."""


class OntAssignmentConstraintAuthorizationRequestBlocked(
    OntAssignmentConstraintAuthorizationError
):
    def __init__(self, preview: OntAssignmentConstraintAuthorizationRequestPreview):
        super().__init__("ONT assignment constraint authorization request is blocked")
        self.preview = preview


class OntAssignmentConstraintAuthorizationReviewBlocked(
    OntAssignmentConstraintAuthorizationError
):
    def __init__(self, preview: OntAssignmentConstraintAuthorizationReviewPreview):
        super().__init__("ONT assignment constraint authorization review is blocked")
        self.preview = preview


@dataclass(frozen=True)
class OntAssignmentConstraintAuthorizationRequestPreview:
    target_environment: str
    coverage_report_sha256: str
    cutover_report_sha256: str
    coverage_payload: dict[str, object]
    expires_at: datetime
    requested_by: str
    reason: str
    request_sha256: str
    blockers: tuple[dict[str, object], ...]
    existing_request_id: uuid.UUID | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "blockers": list(self.blockers),
            "coverage_report_sha256": self.coverage_report_sha256,
            "cutover_report_sha256": self.cutover_report_sha256,
            "existing_request_id": (
                str(self.existing_request_id) if self.existing_request_id else None
            ),
            "expires_at": self.expires_at.isoformat(),
            "ready": self.ready,
            "reason": self.reason,
            "request_sha256": self.request_sha256,
            "requested_by": self.requested_by,
            "target_environment": self.target_environment,
        }


@dataclass(frozen=True)
class OntAssignmentConstraintAuthorizationRequestResult:
    request: OntAssignmentConstraintAuthorizationRequest
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "authorization_request_id": str(self.request.id),
            "coverage_report_sha256": self.request.coverage_report_sha256,
            "created": self.created,
            "cutover_report_sha256": self.request.cutover_report_sha256,
            "expires_at": _utc(self.request.expires_at).isoformat(),
            "request_sha256": self.request.request_sha256,
            "target_environment": self.request.target_environment,
        }


@dataclass(frozen=True)
class OntAssignmentConstraintAuthorizationReviewPreview:
    authorization_request_id: uuid.UUID
    request_sha256: str
    action: str
    reviewed_by: str
    review_notes: str
    current_coverage_report_sha256: str
    current_cutover_report_sha256: str
    expires_at: datetime
    expired: bool
    evidence_current: bool
    current_coverage_ready: bool
    attestation_sha256: str
    blockers: tuple[dict[str, object], ...]
    existing_review_id: uuid.UUID | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "attestation_sha256": self.attestation_sha256,
            "authorization_request_id": str(self.authorization_request_id),
            "blockers": list(self.blockers),
            "current_coverage_ready": self.current_coverage_ready,
            "current_coverage_report_sha256": (self.current_coverage_report_sha256),
            "current_cutover_report_sha256": self.current_cutover_report_sha256,
            "evidence_current": self.evidence_current,
            "existing_review_id": (
                str(self.existing_review_id) if self.existing_review_id else None
            ),
            "expired": self.expired,
            "expires_at": self.expires_at.isoformat(),
            "ready": self.ready,
            "request_sha256": self.request_sha256,
            "review_notes": self.review_notes,
            "reviewed_by": self.reviewed_by,
        }


@dataclass(frozen=True)
class OntAssignmentConstraintAuthorizationReviewResult:
    review: OntAssignmentConstraintAuthorizationReview
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.review.action,
            "attestation_sha256": self.review.attestation_sha256,
            "authorization_request_id": str(self.review.authorization_request_id),
            "created": self.created,
            "review_id": str(self.review.id),
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OntAssignmentConstraintAuthorizationError(f"{field} is required")
    if len(normalized) > limit:
        raise OntAssignmentConstraintAuthorizationError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _sha256(value: object, field: str) -> str:
    normalized = _required_text(value, field, limit=64).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise OntAssignmentConstraintAuthorizationError(
            f"{field} must be a lowercase-compatible SHA-256 digest"
        )
    return normalized


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OntAssignmentConstraintAuthorizationError(
            f"{field} must be a UUID"
        ) from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise OntAssignmentConstraintAuthorizationError(
                f"{field} must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None:
        raise OntAssignmentConstraintAuthorizationError(
            f"{field} must include a timezone"
        )
    return parsed.astimezone(UTC)


def _clock(now: datetime | None) -> datetime:
    return _utc(now) if now is not None else datetime.now(UTC)


def _coverage(db: Session) -> OntAssignmentCutoverCoverageReport:
    try:
        return reconcile_ont_assignment_cutover_coverage(db)
    except (
        OntAssignmentCutoverCoverageError,
        OntAssignmentCutoverVerificationError,
    ) as exc:
        raise OntAssignmentConstraintAuthorizationError(str(exc)) from exc


def _request_payload(
    *,
    target_environment: str,
    coverage: OntAssignmentCutoverCoverageReport,
    expires_at: datetime,
    requested_by: str,
    reason: str,
) -> dict[str, object]:
    return {
        "coverage_payload": coverage.to_dict(),
        "coverage_report_sha256": coverage.coverage_report_sha256,
        "cutover_report_sha256": coverage.cutover_report_sha256,
        "expires_at": expires_at.isoformat(),
        "reason": reason,
        "requested_by": requested_by,
        "schema_version": 1,
        "target_environment": target_environment,
    }


def _stored_request_payload(
    request: OntAssignmentConstraintAuthorizationRequest,
) -> dict[str, object]:
    return {
        "coverage_payload": request.coverage_payload,
        "coverage_report_sha256": request.coverage_report_sha256,
        "cutover_report_sha256": request.cutover_report_sha256,
        "expires_at": _utc(request.expires_at).isoformat(),
        "reason": request.reason,
        "requested_by": request.requested_by,
        "schema_version": 1,
        "target_environment": request.target_environment,
    }


def _review_payload(
    *,
    request: OntAssignmentConstraintAuthorizationRequest,
    action: str,
    reviewed_by: str,
    review_notes: str,
    coverage: OntAssignmentCutoverCoverageReport,
) -> dict[str, object]:
    return {
        "action": action,
        "authorization_request_id": str(request.id),
        "current_coverage_report_sha256": coverage.coverage_report_sha256,
        "current_cutover_report_sha256": coverage.cutover_report_sha256,
        "request_sha256": request.request_sha256,
        "review_notes": review_notes,
        "reviewed_by": reviewed_by,
        "schema_version": 1,
    }


def _stored_review_payload(
    review: OntAssignmentConstraintAuthorizationReview,
) -> dict[str, object]:
    return {
        "action": review.action,
        "authorization_request_id": str(review.authorization_request_id),
        "current_coverage_report_sha256": (review.current_coverage_report_sha256),
        "current_cutover_report_sha256": review.current_cutover_report_sha256,
        "request_sha256": review.request_sha256,
        "review_notes": review.review_notes,
        "reviewed_by": review.reviewed_by,
        "schema_version": 1,
    }


def _load_request(
    db: Session,
    authorization_request_id: object,
    *,
    for_update: bool = False,
) -> OntAssignmentConstraintAuthorizationRequest:
    statement = select(OntAssignmentConstraintAuthorizationRequest).where(
        OntAssignmentConstraintAuthorizationRequest.id
        == _uuid(authorization_request_id, "authorization_request_id")
    )
    if for_update:
        statement = statement.with_for_update()
    request = db.scalar(statement)
    if request is None:
        raise OntAssignmentConstraintAuthorizationError(
            "ONT assignment constraint authorization request not found"
        )
    if _digest(_stored_request_payload(request)) != request.request_sha256:
        raise OntAssignmentConstraintAuthorizationError(
            "stored constraint authorization request evidence is invalid"
        )
    return request


def _review_for_request(
    db: Session,
    request_id: uuid.UUID,
) -> OntAssignmentConstraintAuthorizationReview | None:
    return db.scalar(
        select(OntAssignmentConstraintAuthorizationReview).where(
            OntAssignmentConstraintAuthorizationReview.authorization_request_id
            == request_id
        )
    )


def preview_ont_assignment_constraint_authorization_request(
    db: Session,
    *,
    expected_coverage_report_sha256: object,
    expected_cutover_report_sha256: object,
    target_environment: object,
    expires_at: object,
    requested_by: object,
    reason: object,
    now: datetime | None = None,
) -> OntAssignmentConstraintAuthorizationRequestPreview:
    """Build an exact clean-snapshot request without writing."""

    expected_coverage = _sha256(
        expected_coverage_report_sha256, "expected_coverage_report_sha256"
    )
    expected_cutover = _sha256(
        expected_cutover_report_sha256, "expected_cutover_report_sha256"
    )
    target = _required_text(target_environment, "target_environment", limit=255)
    actor = _required_text(requested_by, "requested_by", limit=160)
    request_reason = _required_text(reason, "reason", limit=4000)
    expiry = _datetime(expires_at, "expires_at")
    current_time = _clock(now)
    coverage = _coverage(db)
    blockers: list[dict[str, object]] = []
    if coverage.coverage_report_sha256 != expected_coverage:
        blockers.append(
            {
                "code": "coverage_report_changed",
                "current_coverage_report_sha256": coverage.coverage_report_sha256,
                "expected_coverage_report_sha256": expected_coverage,
            }
        )
    if coverage.cutover_report_sha256 != expected_cutover:
        blockers.append(
            {
                "code": "cutover_report_changed",
                "current_cutover_report_sha256": coverage.cutover_report_sha256,
                "expected_cutover_report_sha256": expected_cutover,
            }
        )
    if not coverage.ready_for_constraint_authorization_review:
        blockers.append({"code": "coverage_not_ready_for_authorization_review"})
    if expiry <= current_time:
        blockers.append(
            {
                "code": "authorization_request_expiry_not_future",
                "current_time": current_time.isoformat(),
                "expires_at": expiry.isoformat(),
            }
        )
    payload = _request_payload(
        target_environment=target,
        coverage=coverage,
        expires_at=expiry,
        requested_by=actor,
        reason=request_reason,
    )
    request_sha256 = _digest(payload)
    existing = db.scalar(
        select(OntAssignmentConstraintAuthorizationRequest).where(
            OntAssignmentConstraintAuthorizationRequest.request_sha256 == request_sha256
        )
    )
    return OntAssignmentConstraintAuthorizationRequestPreview(
        target_environment=target,
        coverage_report_sha256=coverage.coverage_report_sha256,
        cutover_report_sha256=coverage.cutover_report_sha256,
        coverage_payload=coverage.to_dict(),
        expires_at=expiry,
        requested_by=actor,
        reason=request_reason,
        request_sha256=request_sha256,
        blockers=tuple(blockers),
        existing_request_id=existing.id if existing else None,
    )


def request_ont_assignment_constraint_authorization(
    db: Session,
    *,
    expected_coverage_report_sha256: object,
    expected_cutover_report_sha256: object,
    expected_request_sha256: object,
    target_environment: object,
    expires_at: object,
    requested_by: object,
    reason: object,
    now: datetime | None = None,
) -> OntAssignmentConstraintAuthorizationRequestResult:
    """Persist one immutable exact request; never run constraint DDL."""

    expected_request = _sha256(expected_request_sha256, "expected_request_sha256")
    current_time = _clock(now)
    preview = preview_ont_assignment_constraint_authorization_request(
        db,
        expected_coverage_report_sha256=expected_coverage_report_sha256,
        expected_cutover_report_sha256=expected_cutover_report_sha256,
        target_environment=target_environment,
        expires_at=expires_at,
        requested_by=requested_by,
        reason=reason,
        now=current_time,
    )
    if preview.request_sha256 != expected_request:
        raise OntAssignmentConstraintAuthorizationError(
            "constraint authorization request evidence changed after preview"
        )
    if not preview.ready:
        raise OntAssignmentConstraintAuthorizationRequestBlocked(preview)
    if preview.existing_request_id is not None:
        existing = db.get(
            OntAssignmentConstraintAuthorizationRequest,
            preview.existing_request_id,
        )
        if existing is None:
            raise OntAssignmentConstraintAuthorizationError(
                "existing constraint authorization request disappeared"
            )
        return OntAssignmentConstraintAuthorizationRequestResult(
            request=existing, created=False
        )
    request = OntAssignmentConstraintAuthorizationRequest(
        target_environment=preview.target_environment,
        coverage_report_sha256=preview.coverage_report_sha256,
        cutover_report_sha256=preview.cutover_report_sha256,
        coverage_payload=preview.coverage_payload,
        expires_at=preview.expires_at,
        requested_by=preview.requested_by,
        reason=preview.reason,
        request_sha256=preview.request_sha256,
        created_at=current_time,
    )
    try:
        with db.begin_nested():
            db.add(request)
            db.flush()
        db.commit()
        db.refresh(request)
        return OntAssignmentConstraintAuthorizationRequestResult(
            request=request, created=True
        )
    except IntegrityError:
        existing = db.scalar(
            select(OntAssignmentConstraintAuthorizationRequest).where(
                OntAssignmentConstraintAuthorizationRequest.request_sha256
                == preview.request_sha256
            )
        )
        if existing is not None:
            return OntAssignmentConstraintAuthorizationRequestResult(
                request=existing, created=False
            )
        raise


def preview_ont_assignment_constraint_authorization_review(
    db: Session,
    authorization_request_id: object,
    *,
    expected_request_sha256: object,
    action: object,
    reviewed_by: object,
    review_notes: object,
    now: datetime | None = None,
) -> OntAssignmentConstraintAuthorizationReviewPreview:
    """Preview independent review against the current coverage snapshot."""

    ensure_ont_assignment_cutover_repeatable_snapshot(db)
    expected_request = _sha256(expected_request_sha256, "expected_request_sha256")
    normalized_action = _required_text(action, "action", limit=16).lower()
    if normalized_action not in REVIEW_ACTIONS:
        raise OntAssignmentConstraintAuthorizationError(
            "action must be approve or decline"
        )
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    current_time = _clock(now)
    request = _load_request(db, authorization_request_id)
    if request.request_sha256 != expected_request:
        raise OntAssignmentConstraintAuthorizationError(
            "constraint authorization request differs from the expected digest"
        )
    coverage = _coverage(db)
    expired = _utc(request.expires_at) <= current_time
    evidence_current = (
        request.coverage_report_sha256 == coverage.coverage_report_sha256
        and request.cutover_report_sha256 == coverage.cutover_report_sha256
    )
    blockers: list[dict[str, object]] = []
    if request.requested_by == actor:
        blockers.append(
            {
                "code": "authorization_reviewer_not_independent",
                "reviewed_by": actor,
            }
        )
    if normalized_action == "approve":
        if expired:
            blockers.append(
                {
                    "code": "authorization_request_expired",
                    "expires_at": _utc(request.expires_at).isoformat(),
                }
            )
        if not evidence_current:
            blockers.append(
                {
                    "code": "authorization_request_evidence_stale",
                    "current_coverage_report_sha256": (coverage.coverage_report_sha256),
                    "current_cutover_report_sha256": coverage.cutover_report_sha256,
                }
            )
        if not coverage.ready_for_constraint_authorization_review:
            blockers.append({"code": "coverage_not_ready_for_authorization_review"})
    payload = _review_payload(
        request=request,
        action=normalized_action,
        reviewed_by=actor,
        review_notes=notes,
        coverage=coverage,
    )
    attestation_sha256 = _digest(payload)
    existing = _review_for_request(db, request.id)
    if existing is not None and existing.attestation_sha256 != attestation_sha256:
        blockers.append(
            {
                "code": "authorization_request_already_reviewed_differently",
                "review_id": str(existing.id),
            }
        )
    return OntAssignmentConstraintAuthorizationReviewPreview(
        authorization_request_id=request.id,
        request_sha256=request.request_sha256,
        action=normalized_action,
        reviewed_by=actor,
        review_notes=notes,
        current_coverage_report_sha256=coverage.coverage_report_sha256,
        current_cutover_report_sha256=coverage.cutover_report_sha256,
        expires_at=_utc(request.expires_at),
        expired=expired,
        evidence_current=evidence_current,
        current_coverage_ready=(coverage.ready_for_constraint_authorization_review),
        attestation_sha256=attestation_sha256,
        blockers=tuple(blockers),
        existing_review_id=existing.id if existing else None,
    )


def review_ont_assignment_constraint_authorization(
    db: Session,
    authorization_request_id: object,
    *,
    expected_request_sha256: object,
    expected_attestation_sha256: object,
    action: object,
    reviewed_by: object,
    review_notes: object,
    now: datetime | None = None,
) -> OntAssignmentConstraintAuthorizationReviewResult:
    """Persist independent review evidence; never run constraint DDL."""

    expected_attestation = _sha256(
        expected_attestation_sha256, "expected_attestation_sha256"
    )
    expected_request = _sha256(expected_request_sha256, "expected_request_sha256")
    current_time = _clock(now)
    ensure_ont_assignment_cutover_repeatable_snapshot(db)
    request = _load_request(db, authorization_request_id, for_update=True)
    if request.request_sha256 != expected_request:
        raise OntAssignmentConstraintAuthorizationError(
            "constraint authorization request differs from the expected digest"
        )
    existing = _review_for_request(db, request.id)
    if existing is not None and existing.attestation_sha256 == expected_attestation:
        replay_action = _required_text(action, "action", limit=16).lower()
        replay_actor = _required_text(reviewed_by, "reviewed_by", limit=160)
        replay_notes = _required_text(review_notes, "review_notes", limit=4000)
        if (
            existing.action != replay_action
            or existing.reviewed_by != replay_actor
            or existing.review_notes != replay_notes
        ):
            raise OntAssignmentConstraintAuthorizationError(
                "constraint authorization review replay input differs"
            )
        return OntAssignmentConstraintAuthorizationReviewResult(
            review=existing, created=False
        )
    preview = preview_ont_assignment_constraint_authorization_review(
        db,
        request.id,
        expected_request_sha256=expected_request_sha256,
        action=action,
        reviewed_by=reviewed_by,
        review_notes=review_notes,
        now=current_time,
    )
    if preview.attestation_sha256 != expected_attestation:
        raise OntAssignmentConstraintAuthorizationError(
            "constraint authorization review evidence changed after preview"
        )
    if not preview.ready:
        raise OntAssignmentConstraintAuthorizationReviewBlocked(preview)
    review = OntAssignmentConstraintAuthorizationReview(
        authorization_request_id=request.id,
        request_sha256=request.request_sha256,
        current_coverage_report_sha256=(preview.current_coverage_report_sha256),
        current_cutover_report_sha256=preview.current_cutover_report_sha256,
        action=preview.action,
        requested_by=request.requested_by,
        reviewed_by=preview.reviewed_by,
        review_notes=preview.review_notes,
        attestation_sha256=preview.attestation_sha256,
        reviewed_at=current_time,
    )
    try:
        with db.begin_nested():
            db.add(review)
            db.flush()
        db.commit()
        db.refresh(review)
        return OntAssignmentConstraintAuthorizationReviewResult(
            review=review, created=True
        )
    except IntegrityError:
        existing = _review_for_request(db, request.id)
        if (
            existing is not None
            and existing.attestation_sha256 == preview.attestation_sha256
        ):
            return OntAssignmentConstraintAuthorizationReviewResult(
                review=existing, created=False
            )
        raise


def _authorization_state(
    request: OntAssignmentConstraintAuthorizationRequest,
    review: OntAssignmentConstraintAuthorizationReview | None,
    *,
    coverage: OntAssignmentCutoverCoverageReport,
    now: datetime,
) -> tuple[str, bool, bool, bool]:
    request_valid = _digest(_stored_request_payload(request)) == request.request_sha256
    review_valid = review is None or (
        review.request_sha256 == request.request_sha256
        and review.requested_by == request.requested_by
        and (
            review.action != "approve"
            or (
                review.current_coverage_report_sha256 == request.coverage_report_sha256
                and review.current_cutover_report_sha256
                == request.cutover_report_sha256
            )
        )
        and _digest(_stored_review_payload(review)) == review.attestation_sha256
    )
    expired = _utc(request.expires_at) <= now
    evidence_current = (
        request.coverage_report_sha256 == coverage.coverage_report_sha256
        and request.cutover_report_sha256 == coverage.cutover_report_sha256
        and coverage.ready_for_constraint_authorization_review
    )
    if not request_valid or not review_valid:
        return "invalid_evidence", expired, evidence_current, False
    if review is not None and review.action == "decline":
        return "declined", expired, evidence_current, False
    if review is None:
        if expired:
            return "pending_expired", True, evidence_current, False
        if not evidence_current:
            return "pending_stale", False, False, False
        return "awaiting_independent_review", False, True, False
    if expired:
        return "approved_expired", True, evidence_current, False
    if not evidence_current:
        return "approved_stale", False, False, False
    return "approved_current_evidence", False, True, True


def inspect_ont_assignment_constraint_authorizations(
    db: Session,
    *,
    target_environment: object | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Project current applicability of immutable authorization evidence."""

    ensure_ont_assignment_cutover_repeatable_snapshot(db)
    current_time = _clock(now)
    coverage = _coverage(db)
    statement = select(OntAssignmentConstraintAuthorizationRequest).order_by(
        OntAssignmentConstraintAuthorizationRequest.created_at.desc(),
        OntAssignmentConstraintAuthorizationRequest.id.desc(),
    )
    target = None
    if target_environment not in (None, ""):
        target = _required_text(target_environment, "target_environment", limit=255)
        statement = statement.where(
            OntAssignmentConstraintAuthorizationRequest.target_environment == target
        )
    rows: list[dict[str, object]] = []
    current_approval_count = 0
    for request in db.scalars(statement):
        review = _review_for_request(db, request.id)
        state, expired, evidence_current, eligible = _authorization_state(
            request,
            review,
            coverage=coverage,
            now=current_time,
        )
        if eligible:
            current_approval_count += 1
        rows.append(
            {
                "created_at": _utc(request.created_at).isoformat(),
                "coverage_report_sha256": request.coverage_report_sha256,
                "cutover_report_sha256": request.cutover_report_sha256,
                "eligible_for_separate_ddl_change_review": eligible,
                "evidence_current": evidence_current,
                "expired": expired,
                "expires_at": _utc(request.expires_at).isoformat(),
                "id": str(request.id),
                "reason": request.reason,
                "request_sha256": request.request_sha256,
                "requested_by": request.requested_by,
                "review": (
                    {
                        "action": review.action,
                        "attestation_sha256": review.attestation_sha256,
                        "id": str(review.id),
                        "review_notes": review.review_notes,
                        "reviewed_at": _utc(review.reviewed_at).isoformat(),
                        "reviewed_by": review.reviewed_by,
                    }
                    if review
                    else None
                ),
                "state": state,
                "target_environment": request.target_environment,
            }
        )
    actor_labels = resolve_actor_display_names(
        db,
        {
            actor
            for row in rows
            for actor in (
                row.get("requested_by"),
                (row.get("review") or {}).get("reviewed_by"),
            )
            if actor
        },
    )
    for row in rows:
        row["requested_by_name"] = actor_labels.get(
            str(row.get("requested_by") or ""), "Former or unknown user"
        )
        if row.get("review"):
            row["review"]["reviewed_by_name"] = actor_labels.get(
                str(row["review"].get("reviewed_by") or ""),
                "Former or unknown user",
            )
    return {
        "authorizations": rows,
        "current_approval_count": current_approval_count,
        "current_coverage": {
            "coverage_report_sha256": coverage.coverage_report_sha256,
            "cutover_report_sha256": coverage.cutover_report_sha256,
            "ready_for_constraint_authorization_review": (
                coverage.ready_for_constraint_authorization_review
            ),
        },
        "ddl_authority": False,
        "schema_version": 1,
        "target_environment": target,
    }


__all__ = [
    "OntAssignmentConstraintAuthorizationError",
    "OntAssignmentConstraintAuthorizationRequestBlocked",
    "OntAssignmentConstraintAuthorizationRequestPreview",
    "OntAssignmentConstraintAuthorizationRequestResult",
    "OntAssignmentConstraintAuthorizationReviewBlocked",
    "OntAssignmentConstraintAuthorizationReviewPreview",
    "OntAssignmentConstraintAuthorizationReviewResult",
    "inspect_ont_assignment_constraint_authorizations",
    "preview_ont_assignment_constraint_authorization_request",
    "preview_ont_assignment_constraint_authorization_review",
    "request_ont_assignment_constraint_authorization",
    "review_ont_assignment_constraint_authorization",
]
