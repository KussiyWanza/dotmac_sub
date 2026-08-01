"""Reviewed correction of an accidentally activated subscription.

The public coordinator atomically cancels one mistaken active subscription,
restores one explicitly selected prior subscription, rebinds the subscriber's
single active access credential, and clears stale FUP runtime state.  External
RADIUS and IP state remain projections of the committed lifecycle events.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine
from app.models.catalog import (
    AccessCredential,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock
from app.models.fup_state import FupActionStatus, FupState
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.access_credential_binding import (
    BindAccessCredentialCommand,
    stage_access_credential_binding,
)
from app.services.account_lifecycle import (
    cancel_subscription,
    transition_subscription_status,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.form_contracts import FormConsequence, FormContract
from app.services.form_contracts import register as register_form_contract
from app.services.fup_state import ClearFupRuntimeState, fup_state
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_OWNER = "access.subscription_correction"
_CONCERN = "atomic mistaken-subscription correction coordination"
_SOURCE = "admin:subscription_correction"
_IDEMPOTENCY_SCOPE = "subscription_correction"
_RESTORABLE = frozenset(
    {
        SubscriptionStatus.blocked,
        SubscriptionStatus.disabled,
        SubscriptionStatus.stopped,
        SubscriptionStatus.suspended,
    }
)

SUBSCRIPTION_CORRECTION_FORM = register_form_contract(
    FormContract(
        key="admin.subscription_correction",
        title="Correct mistaken subscription",
        entity="subscription",
        command_owner=_OWNER,
        consequences=(
            FormConsequence(
                key="lifecycle",
                label=(
                    "The mistaken active subscription is canceled and retained for "
                    "audit; the reviewed prior subscription is restored"
                ),
            ),
            FormConsequence(
                key="access",
                label=(
                    "The subscriber's single PPPoE credential moves to the restored "
                    "subscription and its exact RADIUS speed profile"
                ),
            ),
            FormConsequence(
                key="runtime_state",
                label=(
                    "Stale FUP state is cleared and committed lifecycle events request "
                    "RADIUS and IP reconciliation"
                ),
            ),
            FormConsequence(
                key="finance",
                label=(
                    "Existing financial history is preserved; the correction never "
                    "creates an automatic credit or invoice adjustment"
                ),
            ),
        ),
    )
)


class SubscriptionCorrectionError(DomainError):
    """Stable failure at the subscription-correction boundary."""


def _error(suffix: str, message: str, **details: object) -> SubscriptionCorrectionError:
    return SubscriptionCorrectionError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


@dataclass(frozen=True, slots=True)
class SubscriptionCorrectionCandidate:
    subscription_id: UUID
    offer_name: str
    status: SubscriptionStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriptionCorrectionIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SubscriptionCorrectionPreview:
    active_subscription_id: UUID
    active_offer_name: str
    target_subscription_id: UUID
    target_offer_name: str
    target_status: SubscriptionStatus
    target_created_at: datetime
    credential_id: UUID | None
    credential_username: str | None
    target_radius_profile_id: UUID | None
    target_radius_profile_name: str | None
    target_speed_label: str | None
    active_fup_status: FupActionStatus | None
    target_fup_status: FupActionStatus | None
    target_lock_reasons: tuple[str, ...]
    active_invoice_line_count: int
    active_invoice_statuses: tuple[str, ...]
    issues: tuple[SubscriptionCorrectionIssue, ...]
    fingerprint: str

    @property
    def eligible(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class CorrectSubscriptionCommand:
    context: CommandContext
    active_subscription_id: UUID
    target_subscription_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class CorrectSubscriptionOutcome:
    active_subscription_id: UUID
    target_subscription_id: UUID
    credential_id: UUID
    radius_profile_id: UUID
    cleared_fup_subscription_ids: tuple[UUID, ...]
    replayed: bool


def _uuid(value: UUID | str, *, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(
            f"invalid_{field}", f"{field.replace('_', ' ').title()} is invalid."
        ) from exc


def list_correction_candidates(
    db: Session, active_subscription_id: UUID | str
) -> tuple[SubscriptionCorrectionCandidate, ...]:
    """List explicit restorable siblings; never guess which plan is correct."""
    resolved_id = _uuid(active_subscription_id, field="active_subscription_id")
    active = db.get(Subscription, resolved_id)
    if active is None or active.status is not SubscriptionStatus.active:
        return ()
    rows = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == active.subscriber_id)
            .where(Subscription.id != active.id)
            .where(Subscription.created_at < active.created_at)
            .where(Subscription.status.in_(_RESTORABLE))
            .order_by(Subscription.updated_at.desc(), Subscription.id)
        ).all()
    )
    return tuple(
        SubscriptionCorrectionCandidate(
            subscription_id=row.id,
            offer_name=row.offer.name if row.offer else "Unknown plan",
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    )


def _target_profile(
    db: Session, target: Subscription, *, lock: bool
) -> tuple[RadiusProfile | None, SubscriptionCorrectionIssue | None]:
    if target.radius_profile_id is not None:
        statement = select(RadiusProfile).where(
            RadiusProfile.id == target.radius_profile_id
        )
        if lock:
            statement = statement.with_for_update()
        profile = db.scalar(statement)
        if profile is None:
            return None, SubscriptionCorrectionIssue(
                "radius_profile_missing",
                "The target subscription's RADIUS profile no longer exists.",
            )
        if not profile.is_active:
            return None, SubscriptionCorrectionIssue(
                "radius_profile_inactive",
                "The target subscription's RADIUS profile is inactive.",
            )
        return profile, None

    statement = (
        select(RadiusProfile)
        .join(OfferRadiusProfile, OfferRadiusProfile.profile_id == RadiusProfile.id)
        .where(OfferRadiusProfile.offer_id == target.offer_id)
        .where(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.id)
    )
    if lock:
        statement = statement.with_for_update()
    profiles = list(db.scalars(statement).all())
    if not profiles:
        return None, SubscriptionCorrectionIssue(
            "radius_profile_missing",
            "The target plan has no active RADIUS profile.",
        )
    if len(profiles) > 1:
        return None, SubscriptionCorrectionIssue(
            "radius_profile_ambiguous",
            "The target plan has more than one active RADIUS profile.",
        )
    return profiles[0], None


def _speed_label(profile: RadiusProfile | None) -> str | None:
    if profile is None:
        return None
    if profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if profile.download_speed is not None or profile.upload_speed is not None:
        down = (profile.download_speed or 0) / 1000
        up = (profile.upload_speed or 0) / 1000
        return f"{down:g} Mbps down / {up:g} Mbps up"
    return None


def _profile_configuration_issue(
    profile: RadiusProfile | None,
) -> SubscriptionCorrectionIssue | None:
    if profile is None:
        return None
    configured_rate = bool((profile.mikrotik_rate_limit or "").strip())
    configured_speed = bool(profile.download_speed) or bool(profile.upload_speed)
    invalid_speed = any(
        speed is not None and speed < 0
        for speed in (profile.download_speed, profile.upload_speed)
    )
    if invalid_speed:
        return SubscriptionCorrectionIssue(
            "radius_profile_speed_invalid",
            "The target RADIUS profile has an invalid speed configuration.",
        )
    if not configured_rate and not configured_speed:
        return SubscriptionCorrectionIssue(
            "radius_profile_speed_unconfigured",
            "The target RADIUS profile has no enforceable speed configuration.",
        )
    return None


def _ip_projection_issues(
    subscription: Subscription, *, label: str
) -> tuple[SubscriptionCorrectionIssue, ...]:
    issues: list[SubscriptionCorrectionIssue] = []
    for version in (4, 6):
        field = f"ipv{version}_address"
        value = getattr(subscription, field)
        if not value:
            continue
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed.version != version:
            issues.append(
                SubscriptionCorrectionIssue(
                    f"{label}_ipv{version}_invalid",
                    f"The {label} subscription has an invalid stored IPv{version} address; repair its IP evidence before correction.",
                )
            )
    return tuple(issues)


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _build_preview(
    db: Session,
    *,
    active_subscription_id: UUID,
    target_subscription_id: UUID,
    lock: bool,
) -> SubscriptionCorrectionPreview:
    if active_subscription_id == target_subscription_id:
        raise _error(
            "same_subscription", "The active and target subscriptions must differ."
        )
    ids = tuple(sorted((active_subscription_id, target_subscription_id), key=str))
    statement = select(Subscription).where(Subscription.id.in_(ids))
    if lock:
        statement = statement.with_for_update()
    rows = {row.id: row for row in db.scalars(statement).all()}
    active = rows.get(active_subscription_id)
    target = rows.get(target_subscription_id)
    if active is None or target is None:
        raise _error("subscription_not_found", "A selected subscription was not found.")

    issues: list[SubscriptionCorrectionIssue] = []
    if active.subscriber_id != target.subscriber_id:
        issues.append(
            SubscriptionCorrectionIssue(
                "account_mismatch",
                "Both subscriptions must belong to the same subscriber.",
            )
        )
    if active.status is not SubscriptionStatus.active:
        issues.append(
            SubscriptionCorrectionIssue(
                "active_subscription_required",
                "The mistaken subscription is no longer active; refresh before retrying.",
            )
        )
    if target.status not in _RESTORABLE:
        issues.append(
            SubscriptionCorrectionIssue(
                "target_not_restorable",
                "The selected target subscription is not in a restorable state.",
            )
        )
    if _as_utc(target.created_at) >= _as_utc(active.created_at):
        issues.append(
            SubscriptionCorrectionIssue(
                "target_not_prior",
                "The selected target must predate the mistaken active subscription.",
            )
        )

    subscriber_statement = select(Subscriber).where(
        Subscriber.id == active.subscriber_id
    )
    if lock:
        subscriber_statement = subscriber_statement.with_for_update()
    subscriber = db.scalar(subscriber_statement)
    if subscriber is None or not subscriber.billing_enabled:
        issues.append(
            SubscriptionCorrectionIssue(
                "billing_approval_required",
                "Account billing approval is required before restoring service.",
            )
        )
    elif (
        subscriber.lifecycle_override_status is not None
        and subscriber.lifecycle_override_status is not SubscriberStatus.active
    ):
        issues.append(
            SubscriptionCorrectionIssue(
                "account_lifecycle_override",
                "The account has a separate lifecycle hold that must be reviewed first.",
            )
        )

    invoice_statement = (
        select(Invoice.status)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(InvoiceLine.subscription_id == active.id)
    )
    if lock:
        invoice_statement = invoice_statement.with_for_update()
    invoice_rows = list(db.execute(invoice_statement).all())
    invoice_statuses = tuple(
        sorted({str(getattr(row.status, "value", row.status)) for row in invoice_rows})
    )
    if invoice_rows:
        issues.append(
            SubscriptionCorrectionIssue(
                "financial_history_present",
                "The mistaken subscription has invoice lines; reconcile billing before changing service ownership.",
            )
        )

    credential_statement = (
        select(AccessCredential)
        .where(AccessCredential.subscriber_id == active.subscriber_id)
        .where(AccessCredential.is_active.is_(True))
        .order_by(AccessCredential.id)
    )
    if lock:
        credential_statement = credential_statement.with_for_update()
    credentials = list(db.scalars(credential_statement).all())
    credential = credentials[0] if len(credentials) == 1 else None
    if not credentials:
        issues.append(
            SubscriptionCorrectionIssue(
                "credential_missing", "The subscriber has no active access credential."
            )
        )
    elif len(credentials) > 1:
        issues.append(
            SubscriptionCorrectionIssue(
                "credential_ambiguous",
                "The subscriber has multiple active access credentials; select the binding manually first.",
            )
        )
    elif credential is not None and credential.subscription_id not in {
        None,
        active.id,
        target.id,
    }:
        issues.append(
            SubscriptionCorrectionIssue(
                "credential_binding_conflict",
                "The active credential is bound to a third subscription.",
            )
        )
    if credential is not None:
        credential_username = (credential.username or "").strip()
        if not credential_username:
            issues.append(
                SubscriptionCorrectionIssue(
                    "credential_username_missing",
                    "The active access credential has no PPPoE username.",
                )
            )
        if not (target.login or "").strip():
            issues.append(
                SubscriptionCorrectionIssue(
                    "target_login_missing",
                    "The target subscription has no PPPoE login.",
                )
            )
        elif credential_username and credential_username != target.login:
            issues.append(
                SubscriptionCorrectionIssue(
                    "credential_target_login_mismatch",
                    "The active credential username does not match the target subscription login.",
                )
            )
        if (active.login or "").strip() and credential_username != active.login:
            issues.append(
                SubscriptionCorrectionIssue(
                    "credential_active_login_mismatch",
                    "The active credential username does not match the mistaken subscription login.",
                )
            )

    profile, profile_issue = _target_profile(db, target, lock=lock)
    if profile_issue is not None:
        issues.append(profile_issue)
    profile_configuration_issue = _profile_configuration_issue(profile)
    if profile_configuration_issue is not None:
        issues.append(profile_configuration_issue)

    issues.extend(_ip_projection_issues(active, label="active"))
    issues.extend(_ip_projection_issues(target, label="target"))

    fup_statement = select(FupState).where(
        FupState.subscription_id.in_((active.id, target.id))
    )
    if lock:
        fup_statement = fup_statement.with_for_update()
    fup_rows = {row.subscription_id: row for row in db.scalars(fup_statement).all()}
    active_fup = fup_rows.get(active.id)
    target_fup = fup_rows.get(target.id)
    enforcement_statement = select(EnforcementLock).where(
        EnforcementLock.subscription_id == target.id,
        EnforcementLock.is_active.is_(True),
    )
    if lock:
        enforcement_statement = enforcement_statement.with_for_update()
    lock_reasons = tuple(
        sorted(
            {
                lock_row.reason.value
                for lock_row in db.scalars(enforcement_statement).all()
            }
        )
    )
    if lock_reasons:
        issues.append(
            SubscriptionCorrectionIssue(
                "target_enforcement_lock_present",
                "The target subscription has an active enforcement lock that must be resolved by its owner before correction.",
            )
        )
    evidence: dict[str, object] = {
        "active_subscription_id": active.id,
        "active_status": active.status.value,
        "active_updated_at": active.updated_at,
        "active_login": active.login,
        "active_ipv4_address": active.ipv4_address,
        "active_ipv6_address": active.ipv6_address,
        "target_subscription_id": target.id,
        "target_status": target.status.value,
        "target_updated_at": target.updated_at,
        "target_login": target.login,
        "target_ipv4_address": target.ipv4_address,
        "target_ipv6_address": target.ipv6_address,
        "credential_id": credential.id if credential else None,
        "credential_username": credential.username if credential else None,
        "credential_subscription_id": credential.subscription_id
        if credential
        else None,
        "credential_profile_id": credential.radius_profile_id if credential else None,
        "target_profile_id": profile.id if profile else None,
        "target_profile_updated_at": profile.updated_at if profile else None,
        "target_profile_download_speed": profile.download_speed if profile else None,
        "target_profile_upload_speed": profile.upload_speed if profile else None,
        "target_profile_rate_limit": profile.mikrotik_rate_limit if profile else None,
        "active_fup": active_fup.action_status.value if active_fup else None,
        "target_fup": target_fup.action_status.value if target_fup else None,
        "target_locks": lock_reasons,
        "invoice_statuses": invoice_statuses,
        "invoice_line_count": len(invoice_rows),
        "issues": tuple(issue.code for issue in issues),
    }
    return SubscriptionCorrectionPreview(
        active_subscription_id=active.id,
        active_offer_name=active.offer.name if active.offer else "Unknown plan",
        target_subscription_id=target.id,
        target_offer_name=target.offer.name if target.offer else "Unknown plan",
        target_status=target.status,
        target_created_at=target.created_at,
        credential_id=credential.id if credential else None,
        credential_username=credential.username if credential else None,
        target_radius_profile_id=profile.id if profile else None,
        target_radius_profile_name=profile.name if profile else None,
        target_speed_label=_speed_label(profile),
        active_fup_status=active_fup.action_status if active_fup else None,
        target_fup_status=target_fup.action_status if target_fup else None,
        target_lock_reasons=lock_reasons,
        active_invoice_line_count=len(invoice_rows),
        active_invoice_statuses=invoice_statuses,
        issues=tuple(issues),
        fingerprint=_fingerprint(evidence),
    )


def preview_subscription_correction(
    db: Session,
    *,
    active_subscription_id: UUID | str,
    target_subscription_id: UUID | str,
) -> SubscriptionCorrectionPreview:
    return _build_preview(
        db,
        active_subscription_id=_uuid(
            active_subscription_id, field="active_subscription_id"
        ),
        target_subscription_id=_uuid(
            target_subscription_id, field="target_subscription_id"
        ),
        lock=False,
    )


def _correct(
    db: Session, command: CorrectSubscriptionCommand
) -> CorrectSubscriptionOutcome:
    key = str(command.context.idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise _error(
            "invalid_idempotency_key", "A valid correction idempotency key is required."
        )
    existing = db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    )
    if existing is not None:
        if existing.ref_id != command.preview_fingerprint:
            raise _error(
                "idempotency_conflict",
                "This idempotency key was already used for a different correction.",
            )
        current = _build_preview(
            db,
            active_subscription_id=command.active_subscription_id,
            target_subscription_id=command.target_subscription_id,
            lock=False,
        )
        if current.credential_id is None or current.target_radius_profile_id is None:
            raise _error(
                "replay_state_missing", "The prior correction cannot be reconstructed."
            )
        return CorrectSubscriptionOutcome(
            active_subscription_id=command.active_subscription_id,
            target_subscription_id=command.target_subscription_id,
            credential_id=current.credential_id,
            radius_profile_id=current.target_radius_profile_id,
            cleared_fup_subscription_ids=(),
            replayed=True,
        )

    preview = _build_preview(
        db,
        active_subscription_id=command.active_subscription_id,
        target_subscription_id=command.target_subscription_id,
        lock=True,
    )
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "preview_changed",
            "Subscription, billing, credential, or FUP state changed after preview; review again.",
        )
    if not preview.eligible:
        raise _error(
            "correction_ineligible",
            preview.issues[0].message,
            issues=tuple(issue.code for issue in preview.issues),
        )
    assert preview.credential_id is not None
    assert preview.target_radius_profile_id is not None

    active = db.get(Subscription, command.active_subscription_id)
    assert active is not None
    db.add(
        IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=active.subscriber_id,
            ref_id=command.preview_fingerprint,
        )
    )
    db.flush()

    cancel_subscription(
        db,
        str(command.active_subscription_id),
        "Mistaken subscription replaced by reviewed correction",
        _SOURCE,
        emit=True,
        generate_credit=False,
    )
    restored = transition_subscription_status(
        db,
        str(command.target_subscription_id),
        SubscriptionStatus.active,
        reason=command.context.reason,
        source=_SOURCE,
        emit=True,
    )
    if not restored:
        raise _error(
            "correction_not_applied",
            "The target subscription could not be restored after the mistaken subscription was canceled.",
        )

    credential = stage_access_credential_binding(
        db,
        BindAccessCredentialCommand(
            credential_id=preview.credential_id,
            subscriber_id=active.subscriber_id,
            target_subscription_id=command.target_subscription_id,
            target_radius_profile_id=preview.target_radius_profile_id,
            actor=command.context.actor,
        ),
    )
    cleared: list[UUID] = []
    evaluated_at = datetime.now(UTC)
    for subscription_id in (
        command.active_subscription_id,
        command.target_subscription_id,
    ):
        state = fup_state.clear(
            db,
            ClearFupRuntimeState(
                subscription_id=subscription_id,
                evaluated_at=evaluated_at,
            ),
        )
        if state is not None:
            cleared.append(subscription_id)

    emit_event(
        db,
        EventType.subscription_correction_applied,
        {
            "schema_version": 1,
            "active_subscription_id": str(command.active_subscription_id),
            "target_subscription_id": str(command.target_subscription_id),
            "credential_id": str(credential.id),
            "target_radius_profile_id": str(preview.target_radius_profile_id),
            "cleared_fup_subscription_ids": [str(item) for item in cleared],
            "preview_fingerprint": preview.fingerprint,
        },
        actor=command.context.actor,
        account_id=active.subscriber_id,
        subscription_id=command.target_subscription_id,
    )
    return CorrectSubscriptionOutcome(
        active_subscription_id=command.active_subscription_id,
        target_subscription_id=command.target_subscription_id,
        credential_id=credential.id,
        radius_profile_id=preview.target_radius_profile_id,
        cleared_fup_subscription_ids=tuple(cleared),
        replayed=False,
    )


def correct_subscription(
    db: Session, command: CorrectSubscriptionCommand
) -> CorrectSubscriptionOutcome:
    """Execute one reviewed correction in the coordinator-owned transaction."""
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner=_OWNER,
            concern=_CONCERN,
            name="correct_subscription",
        ),
        context=command.context,
        operation=lambda: _correct(db, command),
    )


__all__ = [
    "CorrectSubscriptionCommand",
    "CorrectSubscriptionOutcome",
    "SubscriptionCorrectionCandidate",
    "SubscriptionCorrectionError",
    "SubscriptionCorrectionIssue",
    "SubscriptionCorrectionPreview",
    "correct_subscription",
    "list_correction_candidates",
    "preview_subscription_correction",
]
