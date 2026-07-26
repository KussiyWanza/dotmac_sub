"""Signed, stale-safe confirmation for staff proposed-route review."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError
from sqlalchemy.orm import Session

from app.models.idempotency import IdempotencyKey
from app.services import context_signing
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.vendor_portal_operations import vendor_portal_operations

_TOKEN_TYPE = "vendor_route_review_confirmation"
_TOKEN_ISSUER = "dotmac_sub.vendor_route_review_proposals"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)
_SCOPES = {
    "accept": "vendor_route_accept",
    "reject": "vendor_route_reject",
}

_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner="operations.vendor_route_review_confirmation",
    concern="staff proposed-route review idempotency and replay result",
    name="confirm_vendor_route_review",
)


class VendorRouteReviewConfirmationError(DomainError):
    """Stable rejection from the staff proposed-route review coordinator."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> VendorRouteReviewConfirmationError:
    return VendorRouteReviewConfirmationError(
        code=f"operations.vendor_route_review_confirmation.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class VendorRouteReviewProposal:
    revision_id: str
    project_id: str
    action: str
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    confirmation_label: str
    confirmation_token: str
    expires_at: datetime


@dataclass(frozen=True)
class VendorRouteReviewResult:
    revision_id: str
    project_id: str
    action: str
    review_event_id: str
    replayed: bool


@dataclass(frozen=True)
class ConfirmVendorRouteReviewCommand:
    context: CommandContext
    confirmation_token: str
    revision_id: str
    action: str
    actor_id: str


def _fingerprint(state: dict[str, Any]) -> str:
    encoded = json.dumps(
        state,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def issue_review(
    db: Session,
    *,
    revision_id: str,
    action: str,
    actor_id: str,
    reason: str | None = None,
) -> VendorRouteReviewProposal:
    normalized_actor = str(actor_id or "").strip()
    if not normalized_actor:
        raise _error("actor_required", "Review actor is required.")
    preview = vendor_portal_operations.preview_route_revision_review(
        db,
        revision_id,
        action=action,
        reason=reason,
    )
    issued_at = datetime.now(UTC)
    expires_at = issued_at + _TOKEN_TTL
    claims = {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid.uuid4().hex,
        "revision_id": str(revision_id),
        "project_id": str(preview["project_id"]),
        "action": action,
        "actor_id": normalized_actor,
        "reason": preview["state"]["reason"],
        "state_fingerprint": _fingerprint(preview["state"]),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return VendorRouteReviewProposal(
        revision_id=str(revision_id),
        project_id=str(preview["project_id"]),
        action=action,
        title=str(preview["title"]),
        summary=str(preview["summary"]),
        details=tuple((str(label), str(value)) for label, value in preview["details"]),
        confirmation_label=(
            "Confirm route acceptance"
            if action == "accept"
            else "Confirm route rejection"
        ),
        confirmation_token=context_signing.sign_context_token(db, claims),
        expires_at=expires_at,
    )


def _decode(db: Session, token: str) -> dict[str, Any]:
    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 131_072:
        raise _error("invalid_proposal", "Confirmation proposal is invalid.")
    try:
        claims = context_signing.verify_context_token(db, normalized)
    except JWTError as exc:
        raise _error(
            "expired_proposal",
            "Confirmation proposal is invalid or expired; preview again.",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
        or claims.get("action") not in _SCOPES
    ):
        raise _error("invalid_proposal", "Confirmation proposal is invalid.")
    return claims


def _locked_replay(
    db: Session,
    *,
    scope: str,
    key: str,
) -> IdempotencyKey | None:
    existing = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        if existing.ref_id:
            return existing
        raise _error(
            "confirmation_in_progress",
            "This confirmation is already running.",
        )
    return None


def confirm_review(
    db: Session,
    command: ConfirmVendorRouteReviewCommand,
) -> VendorRouteReviewResult:
    """Confirm one proposed-route decision on a typed root transaction."""

    def operation() -> VendorRouteReviewResult:
        claims = _decode(db, command.confirmation_token)
        if (
            str(claims.get("revision_id") or "") != str(command.revision_id)
            or str(claims.get("action") or "") != str(command.action)
            or str(claims.get("actor_id") or "") != str(command.actor_id)
        ):
            raise _error(
                "proposal_context_mismatch",
                "Confirmation proposal belongs to another review context.",
            )
        key = str(claims.get("jti") or "").strip()
        if not key:
            raise _error("invalid_proposal", "Confirmation proposal is invalid.")
        scope = _SCOPES[command.action]
        prior = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
            .one_or_none()
        )
        if prior is not None and prior.ref_id:
            return VendorRouteReviewResult(
                revision_id=str(command.revision_id),
                project_id=str(claims["project_id"]),
                action=command.action,
                review_event_id=prior.ref_id,
                replayed=True,
            )
        preview = vendor_portal_operations.preview_route_revision_review(
            db,
            command.revision_id,
            action=command.action,
            reason=claims.get("reason"),
            for_update=True,
        )
        replay = _locked_replay(db, scope=scope, key=key)
        if replay is not None:
            return VendorRouteReviewResult(
                revision_id=str(command.revision_id),
                project_id=str(claims["project_id"]),
                action=command.action,
                review_event_id=str(replay.ref_id),
                replayed=True,
            )
        if not hmac.compare_digest(
            str(claims.get("state_fingerprint") or ""),
            _fingerprint(preview["state"]),
        ):
            raise _error(
                "stale_proposal",
                "Proposed route changed after preview; review it again.",
            )
        reservation = IdempotencyKey(scope=scope, key=key)
        db.add(reservation)
        db.flush()
        result = vendor_portal_operations.transition_route_revision_review(
            db,
            command.revision_id,
            action=command.action,
            actor_id=command.actor_id,
            reason=claims.get("reason"),
        )
        result_id = str(result.get("review_event_id") or "")
        if not result_id:
            raise _error(
                "missing_result_evidence",
                "Route review completed without stable result evidence.",
            )
        reservation.ref_id = result_id
        return VendorRouteReviewResult(
            revision_id=str(command.revision_id),
            project_id=str(claims["project_id"]),
            action=command.action,
            review_event_id=result_id,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )
