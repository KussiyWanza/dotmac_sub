"""Signed, stale-safe staff confirmation for vendor supply decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from jose import JWTError
from sqlalchemy.orm import Session

from app.models.idempotency import IdempotencyKey
from app.services import context_signing, vendor_advances, vendor_material_release
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.vendor_supply_views import (
    SupplyReviewPreview,
    VendorSupplyReviewAction,
    VendorSupplyType,
    advance_review_preview,
    material_review_preview,
)

_TOKEN_TYPE = "vendor_supply_review_confirmation"
_TOKEN_ISSUER = "dotmac_sub.vendor_supply_review_proposals"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)
_SUPPLY_TYPES = frozenset(item.value for item in VendorSupplyType)
_ACTIONS = frozenset(item.value for item in VendorSupplyReviewAction)

_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner="operations.vendor_supply_review_confirmation",
    concern="vendor supply review idempotency and replay result",
    name="confirm_vendor_supply_review",
)


class VendorSupplyReviewConfirmationError(DomainError):
    """Stable rejection from the vendor supply review coordinator."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> VendorSupplyReviewConfirmationError:
    return VendorSupplyReviewConfirmationError(
        code=f"operations.vendor_supply_review_confirmation.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class VendorSupplyReviewProposal:
    supply_type: VendorSupplyType
    record_id: UUID
    project_id: UUID
    action: VendorSupplyReviewAction
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    confirmation_label: str
    confirmation_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VendorSupplyReviewResult:
    supply_type: VendorSupplyType
    record_id: UUID
    project_id: UUID
    action: VendorSupplyReviewAction
    replayed: bool


@dataclass(frozen=True, slots=True)
class ConfirmVendorSupplyReviewCommand:
    context: CommandContext
    confirmation_token: str
    supply_type: VendorSupplyType
    record_id: UUID
    action: VendorSupplyReviewAction
    actor_id: UUID


def _fingerprint(state: tuple[tuple[str, str], ...]) -> str:
    encoded = json.dumps(state, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _preview(
    db: Session,
    *,
    supply_type: VendorSupplyType,
    record_id: UUID,
    action: VendorSupplyReviewAction,
    reason: str | None,
    for_update: bool = False,
) -> SupplyReviewPreview:
    try:
        if supply_type is VendorSupplyType.material:
            return material_review_preview(
                db,
                release_id=record_id,
                action=action,
                reason=reason,
                for_update=for_update,
            )
        if supply_type is VendorSupplyType.advance:
            return advance_review_preview(
                db,
                advance_id=record_id,
                action=action,
                reason=reason,
                for_update=for_update,
            )
        raise _error("unsupported_supply_type", "Unsupported vendor supply type.")
    except VendorSupplyReviewConfirmationError:
        raise
    except DomainError as exc:
        raise _error(exc.code.rsplit(".", 1)[-1], exc.message) from exc


def issue_review(
    db: Session,
    *,
    supply_type: VendorSupplyType,
    record_id: UUID,
    action: VendorSupplyReviewAction,
    actor_id: UUID,
    reason: str | None = None,
) -> VendorSupplyReviewProposal:
    normalized_actor = str(actor_id)
    preview = _preview(
        db,
        supply_type=supply_type,
        record_id=record_id,
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
        "supply_type": supply_type.value,
        "record_id": str(preview.record_id),
        "project_id": str(preview.project_id),
        "action": action.value,
        "actor_id": normalized_actor,
        "reason": preview.reason,
        "state_fingerprint": _fingerprint(preview.state),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return VendorSupplyReviewProposal(
        supply_type=supply_type,
        record_id=preview.record_id,
        project_id=preview.project_id,
        action=action,
        title=preview.title,
        summary=preview.summary,
        details=preview.details,
        confirmation_label=f"Confirm {action.value}",
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
        or claims.get("supply_type") not in _SUPPLY_TYPES
        or claims.get("action") not in _ACTIONS
    ):
        raise _error("invalid_proposal", "Confirmation proposal is invalid.")
    return claims


def _review(
    db: Session,
    *,
    supply_type: VendorSupplyType,
    record_id: UUID,
    action: VendorSupplyReviewAction,
    actor_id: UUID,
    reason: str | None,
) -> None:
    try:
        if supply_type is VendorSupplyType.material:
            if action is VendorSupplyReviewAction.approve:
                vendor_material_release.approve(
                    db, record_id, actor_id=actor_id, notes=reason
                )
            else:
                vendor_material_release.reject(
                    db, record_id, actor_id=actor_id, reason=str(reason or "")
                )
        elif action is VendorSupplyReviewAction.approve:
            vendor_advances.approve(db, record_id, actor_id=actor_id, notes=reason)
        else:
            vendor_advances.reject(
                db, record_id, actor_id=actor_id, reason=str(reason or "")
            )
    except ValueError as exc:
        suffix = str(getattr(exc, "code", "") or "review_failed")
        raise _error(suffix, str(exc)) from exc


def confirm_review(
    db: Session,
    command: ConfirmVendorSupplyReviewCommand,
) -> VendorSupplyReviewResult:
    """Confirm one supply decision in the coordinator-owned transaction."""

    def operation() -> VendorSupplyReviewResult:
        claims = _decode(db, command.confirmation_token)
        if (
            str(claims.get("supply_type") or "") != command.supply_type.value
            or str(claims.get("record_id") or "") != str(command.record_id)
            or str(claims.get("action") or "") != command.action.value
            or str(claims.get("actor_id") or "") != str(command.actor_id)
            or str(command.context.scope) != str(command.record_id)
            or str(command.context.actor) != str(command.actor_id)
        ):
            raise _error(
                "proposal_context_mismatch",
                "Confirmation proposal belongs to another review context.",
            )
        key = str(claims.get("jti") or "").strip()
        if not key:
            raise _error("invalid_proposal", "Confirmation proposal is invalid.")
        scope = f"vendor_supply_{command.supply_type.value}_{command.action.value}"
        prior = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
            .one_or_none()
        )
        if prior is not None and prior.ref_id:
            return VendorSupplyReviewResult(
                supply_type=command.supply_type,
                record_id=command.record_id,
                project_id=coerce_uuid(claims.get("project_id")),
                action=command.action,
                replayed=True,
            )
        preview = _preview(
            db,
            supply_type=command.supply_type,
            record_id=command.record_id,
            action=command.action,
            reason=claims.get("reason"),
            for_update=True,
        )
        replay = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
            .with_for_update()
            .one_or_none()
        )
        if replay is not None and replay.ref_id:
            return VendorSupplyReviewResult(
                supply_type=command.supply_type,
                record_id=command.record_id,
                project_id=coerce_uuid(claims.get("project_id")),
                action=command.action,
                replayed=True,
            )
        if replay is not None:
            raise _error(
                "confirmation_in_progress",
                "This confirmation is already running.",
            )
        if not hmac.compare_digest(
            str(claims.get("state_fingerprint") or ""),
            _fingerprint(preview.state),
        ):
            raise _error(
                "stale_proposal",
                "Supply data changed after preview; review it again.",
            )
        reservation = IdempotencyKey(scope=scope, key=key)
        db.add(reservation)
        db.flush()
        _review(
            db,
            supply_type=command.supply_type,
            record_id=command.record_id,
            action=command.action,
            actor_id=command.actor_id,
            reason=claims.get("reason"),
        )
        reservation.ref_id = str(command.record_id)
        return VendorSupplyReviewResult(
            supply_type=command.supply_type,
            record_id=command.record_id,
            project_id=preview.project_id,
            action=command.action,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )
