"""Typed admin service-extension detail and activity projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import SubscriptionStatus
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionReversal,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.system_user import SystemUser
from app.schemas.status_presentation import (
    StatusIcon,
    StatusPresentation,
    StatusTone,
)
from app.services import display_format
from app.services import service_extensions as service_extensions_service
from app.services.audit_adapter import audit_adapter
from app.services.auth_dependencies import has_permission

_ACTIVITY_ACTIONS = {
    "billing.service_extension_created",
    "billing.service_extension_applied",
    "billing.service_extension_canceled",
    "billing.service_extension_reversed",
}


class ServiceExtensionActivityProvenance(StrEnum):
    canonical = "canonical"
    legacy_reconstructed = "legacy_reconstructed"


@dataclass(frozen=True, slots=True)
class ServiceExtensionActivityItem:
    action_label: str
    actor_label: str
    occurred_at: datetime
    occurred_at_display: str
    details: str
    provenance: ServiceExtensionActivityProvenance
    provenance_label: str | None
    tone: StatusTone
    stable_order_key: str


@dataclass(frozen=True, slots=True)
class ServiceExtensionCustomerItem:
    label: str
    account_number: str | None
    email: str | None


@dataclass(frozen=True, slots=True)
class ServiceExtensionSubscriptionItem:
    subscriber_label: str
    login: str | None
    next_billing_at_display: str
    service_status_label: str
    restoration_pending: bool
    previous_billing_display: str
    grant_starts_display: str
    grant_ends_display: str
    anchor_basis_label: str


@dataclass(frozen=True, slots=True)
class ServiceExtensionImpactProjection:
    total_count: int
    extendable_count: int
    skipped_count: int
    decision_message: str
    outcome_message: str | None
    sample_provenance_note: str | None


@dataclass(frozen=True, slots=True)
class ServiceExtensionSummaryProjection:
    id: UUID
    reason: str
    status_presentation: StatusPresentation
    days: int
    scope_label: str
    outage_window_display: str
    created_by_label: str
    created_at_display: str


@dataclass(frozen=True, slots=True)
class ServiceExtensionDetailProjection:
    summary: ServiceExtensionSummaryProjection
    impact: ServiceExtensionImpactProjection
    selected_customers: tuple[ServiceExtensionCustomerItem, ...]
    sample_subscriptions: tuple[ServiceExtensionSubscriptionItem, ...]
    activity: tuple[ServiceExtensionActivityItem, ...]
    reversal: ServiceExtensionReversalSummaryProjection | None
    can_apply: bool
    can_cancel: bool
    can_reverse: bool
    apply_idempotency_key: str
    cancel_idempotency_key: str
    reverse_idempotency_key: str


@dataclass(frozen=True, slots=True)
class CustomerServiceExtensionImpactItem:
    subscription_id: UUID
    previous_billing_display: str
    new_billing_display: str
    grant_starts_display: str
    grant_ends_display: str
    anchor_basis_label: str


@dataclass(frozen=True, slots=True)
class CustomerServiceExtensionItem:
    id: UUID
    reason: str
    status_presentation: StatusPresentation
    created_at_display: str
    outage_window_display: str
    days: int
    scope_label: str
    affected_count: int
    skipped_count: int
    match_basis: service_extensions_service.CustomerServiceExtensionMatchBasis
    impact_message: str
    impacts: tuple[CustomerServiceExtensionImpactItem, ...]


@dataclass(frozen=True, slots=True)
class CustomerServiceExtensionHistory:
    items: tuple[CustomerServiceExtensionItem, ...]
    total_count: int


@dataclass(frozen=True, slots=True)
class ServiceExtensionReversalSummaryProjection:
    reason: str
    reversed_by_label: str
    reversed_at_display: str
    inspected_count: int
    restored_anchor_count: int
    preserved_later_anchor_count: int
    preserved_lower_anchor_count: int
    preserved_terminal_count: int


@dataclass(frozen=True, slots=True)
class ServiceExtensionReversalConfirmationProjection:
    extension_id: UUID
    extension_reason: str
    reversal_reason: str
    days: int
    scope_label: str
    preview: service_extensions_service.ServiceExtensionReversalPreview


_STATUS_PRESENTATIONS = {
    ServiceExtensionStatus.pending: StatusPresentation(
        value=ServiceExtensionStatus.pending.value,
        label="Pending",
        tone=StatusTone.warning,
        icon=StatusIcon.clock,
    ),
    ServiceExtensionStatus.applied: StatusPresentation(
        value=ServiceExtensionStatus.applied.value,
        label="Applied",
        tone=StatusTone.positive,
        icon=StatusIcon.check,
    ),
    ServiceExtensionStatus.canceled: StatusPresentation(
        value=ServiceExtensionStatus.canceled.value,
        label="Canceled",
        tone=StatusTone.neutral,
        icon=StatusIcon.x,
    ),
    ServiceExtensionStatus.reversed: StatusPresentation(
        value=ServiceExtensionStatus.reversed.value,
        label="Reversed",
        tone=StatusTone.negative,
        icon=StatusIcon.x,
    ),
}

_SCOPE_LABELS = {
    ServiceExtensionScope.network: "Whole network",
    ServiceExtensionScope.pop_site: "POP site",
    ServiceExtensionScope.nas_device: "NAS device",
    ServiceExtensionScope.subscribers: "Selected customers",
}

_ACTION_LABELS = {
    "billing.service_extension_created": "Created",
    "billing.service_extension_applied": "Applied",
    "billing.service_extension_canceled": "Canceled",
    "billing.service_extension_reversed": "Reversed",
}

_ACTION_TONES = {
    "billing.service_extension_created": StatusTone.info,
    "billing.service_extension_applied": StatusTone.positive,
    "billing.service_extension_canceled": StatusTone.neutral,
    "billing.service_extension_reversed": StatusTone.negative,
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _staff_label(user: SystemUser | None) -> str | None:
    if user is None:
        return None
    return (
        str(user.display_name or "").strip()
        or f"{user.first_name or ''} {user.last_name or ''}".strip()
        or str(user.email or "").strip()
        or None
    )


def _load_staff_labels(
    db: Session,
    *,
    extension: ServiceExtension,
    events: list[AuditEvent],
) -> dict[str, str]:
    raw_ids = {
        value
        for value in (
            extension.created_by,
            extension.applied_by,
            extension.canceled_by,
            *(
                event.actor_id
                for event in events
                if event.actor_type == AuditActorType.user
            ),
        )
        if value
    }
    ids: set[UUID] = set()
    for value in raw_ids:
        try:
            ids.add(UUID(str(value)))
        except ValueError:
            continue
    if not ids:
        return {}
    return {
        str(user.id): label
        for user in db.scalars(select(SystemUser).where(SystemUser.id.in_(ids))).all()
        if (label := _staff_label(user))
    }


def _actor_label(event: AuditEvent, staff_labels: dict[str, str]) -> str:
    if event.actor_label:
        return event.actor_label
    if event.actor_type == AuditActorType.user:
        return staff_labels.get(str(event.actor_id), "Former staff member")
    if event.actor_type == AuditActorType.api_key:
        return "Integration"
    if event.actor_type == AuditActorType.service:
        return "Automated service"
    return "System"


def _legacy_actor_label(
    actor_id: str | None,
    staff_labels: dict[str, str],
) -> str:
    if not actor_id:
        return "Unknown staff member"
    return staff_labels.get(actor_id, "Former staff member")


def _metadata_count(metadata: dict[object, object], key: str) -> int:
    value = metadata.get(key, 0)
    if not isinstance(value, int | float | str):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _activity_details(event: AuditEvent, extension: ServiceExtension) -> str:
    metadata = event.metadata_ or {}
    if event.action == "billing.service_extension_created":
        return (
            f"{extension.days}-day extension created for "
            f"{_SCOPE_LABELS[extension.scope_type].lower()} scope."
        )
    if event.action == "billing.service_extension_applied":
        affected = _metadata_count(metadata, "affected")
        skipped = _metadata_count(metadata, "skipped")
        resumed = _metadata_count(metadata, "resumed")
        details = f"{affected} extended; {skipped} skipped"
        if resumed:
            details += f"; {resumed} restored"
        return details + "."
    if event.action == "billing.service_extension_reversed":
        restored = _metadata_count(metadata, "anchors_restored")
        preserved = (
            _metadata_count(metadata, "later_anchors_preserved")
            + _metadata_count(metadata, "lower_anchors_preserved")
            + _metadata_count(metadata, "terminal_subscriptions_preserved")
        )
        return (
            f"Grant invalidated; {restored} billing anchor(s) restored and "
            f"{preserved} preserved for safety."
        )
    return "Pending extension canceled without changing subscription validity."


def _canonical_activity(
    db: Session,
    *,
    extension: ServiceExtension,
    events: list[AuditEvent],
    staff_labels: dict[str, str],
) -> list[ServiceExtensionActivityItem]:
    items: list[ServiceExtensionActivityItem] = []
    for event in events:
        if event.action not in _ACTIVITY_ACTIONS:
            continue
        items.append(
            ServiceExtensionActivityItem(
                action_label=_ACTION_LABELS[event.action],
                actor_label=_actor_label(event, staff_labels),
                occurred_at=event.occurred_at,
                occurred_at_display=display_format.format_timestamp(
                    event.occurred_at,
                    db,
                ),
                details=_activity_details(event, extension),
                provenance=ServiceExtensionActivityProvenance.canonical,
                provenance_label=None,
                tone=_ACTION_TONES[event.action],
                stable_order_key=str(event.id),
            )
        )
    return items


def _legacy_activity(
    db: Session,
    *,
    extension: ServiceExtension,
    canonical_actions: set[str],
    staff_labels: dict[str, str],
) -> list[ServiceExtensionActivityItem]:
    items: list[ServiceExtensionActivityItem] = []
    provenance_label = "Reconstructed from legacy lifecycle fields"
    if "billing.service_extension_created" not in canonical_actions:
        items.append(
            ServiceExtensionActivityItem(
                action_label="Created",
                actor_label=_legacy_actor_label(
                    extension.created_by,
                    staff_labels,
                ),
                occurred_at=extension.created_at,
                occurred_at_display=display_format.format_timestamp(
                    extension.created_at,
                    db,
                ),
                details=(
                    f"{extension.days}-day extension created for "
                    f"{_SCOPE_LABELS[extension.scope_type].lower()} scope."
                ),
                provenance=(ServiceExtensionActivityProvenance.legacy_reconstructed),
                provenance_label=provenance_label,
                tone=StatusTone.info,
                stable_order_key="legacy:created",
            )
        )
    if (
        extension.status
        in {ServiceExtensionStatus.applied, ServiceExtensionStatus.reversed}
        and extension.applied_at is not None
        and "billing.service_extension_applied" not in canonical_actions
    ):
        items.append(
            ServiceExtensionActivityItem(
                action_label="Applied",
                actor_label=_legacy_actor_label(
                    extension.applied_by,
                    staff_labels,
                ),
                occurred_at=extension.applied_at,
                occurred_at_display=display_format.format_timestamp(
                    extension.applied_at,
                    db,
                ),
                details=(
                    f"{extension.affected_count} extended; "
                    f"{extension.skipped_count} skipped."
                ),
                provenance=(ServiceExtensionActivityProvenance.legacy_reconstructed),
                provenance_label=provenance_label,
                tone=StatusTone.positive,
                stable_order_key="legacy:applied",
            )
        )
    return items


def _created_actor_label(
    extension: ServiceExtension,
    *,
    events: list[AuditEvent],
    staff_labels: dict[str, str],
) -> str:
    created_event = next(
        (
            event
            for event in events
            if event.action == "billing.service_extension_created"
        ),
        None,
    )
    if created_event is not None:
        return _actor_label(created_event, staff_labels)
    return _legacy_actor_label(extension.created_by, staff_labels)


def _impact_projection(
    db: Session,
    *,
    extension: ServiceExtension,
    preview: service_extensions_service.ServiceExtensionPreview,
) -> ServiceExtensionImpactProjection:
    if extension.status == ServiceExtensionStatus.pending:
        decision_message = (
            f"Applying will extend {preview.extendable_count} subscription(s) by "
            f"{extension.days} day(s)."
        )
        if preview.skipped_count:
            decision_message += (
                f" {preview.skipped_count} will be skipped because no billing "
                "anchor is available."
            )
        outcome_message = None
    elif extension.status == ServiceExtensionStatus.applied:
        decision_message = "This extension has already been applied."
        outcome_message = (
            f"Applied {display_format.format_timestamp(extension.applied_at, db)} "
            f"— {extension.affected_count} subscription(s) extended"
        )
        if extension.skipped_count:
            outcome_message += f"; {extension.skipped_count} skipped"
        outcome_message += "."
    elif extension.status == ServiceExtensionStatus.reversed:
        decision_message = (
            "This extension has been reversed. Its grant intervals no longer "
            "provide service coverage or an enforcement shield."
        )
        outcome_message = None
    else:
        decision_message = "This extension is canceled and cannot be applied."
        outcome_message = None
    return ServiceExtensionImpactProjection(
        total_count=preview.total_count,
        extendable_count=preview.extendable_count,
        skipped_count=preview.skipped_count,
        decision_message=decision_message,
        outcome_message=outcome_message,
        # Pending rows are a proposal recomputed at apply time; applied rows are
        # the immutable intervals actually recorded.
        sample_provenance_note=(
            "proposal calculated at "
            f"{display_format.format_timestamp(preview.previewed_at, db)} "
            "and rechecked when applied"
            if extension.status == ServiceExtensionStatus.pending
            else None
        ),
    )


def build_customer_service_extension_history(
    db: Session,
    *,
    subscriber_ids: tuple[UUID, ...],
    limit: int = 10,
    offset: int = 0,
) -> CustomerServiceExtensionHistory:
    """Project one lifecycle row per extension request for Customer 360."""

    history = service_extensions_service.customer_service_extension_history(
        db,
        subscriber_ids=subscriber_ids,
        limit=limit,
        offset=offset,
    )
    items: list[CustomerServiceExtensionItem] = []
    for record in history.records:
        impacts = tuple(
            CustomerServiceExtensionImpactItem(
                subscription_id=impact.subscription_id,
                previous_billing_display=display_format.format_timestamp(
                    impact.previous_next_billing_at,
                    db,
                    fmt="%b %d, %Y",
                ),
                new_billing_display=display_format.format_timestamp(
                    impact.new_next_billing_at,
                    db,
                    fmt="%b %d, %Y",
                ),
                grant_starts_display=display_format.format_timestamp(
                    impact.grant_starts_at,
                    db,
                    fmt="%b %d, %Y",
                ),
                grant_ends_display=display_format.format_timestamp(
                    impact.grant_ends_at,
                    db,
                    fmt="%b %d, %Y",
                ),
                anchor_basis_label=(
                    impact.anchor_basis.value.replace("_", " ").title()
                    if impact.anchor_basis is not None
                    else "Not recorded"
                ),
            )
            for impact in record.impacts
        )
        if impacts:
            impact_message = (
                f"Billing impact recorded for {len(impacts)} subscription(s)."
            )
        elif record.status == ServiceExtensionStatus.pending:
            impact_message = "Submitted and awaiting approval; no billing change yet."
        elif record.status == ServiceExtensionStatus.canceled:
            impact_message = "Canceled before a billing change was applied."
        elif record.status == ServiceExtensionStatus.reversed:
            impact_message = "Reversed; no customer billing impact row was recorded."
        else:
            impact_message = "Applied with no eligible customer billing anchor changed."
        items.append(
            CustomerServiceExtensionItem(
                id=record.id,
                reason=record.reason,
                status_presentation=_STATUS_PRESENTATIONS[record.status],
                created_at_display=display_format.format_timestamp(
                    record.created_at,
                    db,
                    fmt="%b %d, %Y",
                ),
                outage_window_display=(
                    f"{display_format.format_timestamp(record.window_start, db)} — "
                    f"{display_format.format_timestamp(record.window_end, db)}"
                ),
                days=record.days,
                scope_label=_SCOPE_LABELS[record.scope_type],
                affected_count=record.affected_count,
                skipped_count=record.skipped_count,
                match_basis=record.match_basis,
                impact_message=impact_message,
                impacts=impacts,
            )
        )
    return CustomerServiceExtensionHistory(
        items=tuple(items),
        total_count=history.total_count,
    )


def build_service_extension_detail(
    db: Session,
    *,
    extension_id: UUID,
    auth: dict[str, Any] | None,
) -> ServiceExtensionDetailProjection:
    """Compose the complete L3 detail page from exact authoritative inputs."""

    extension = service_extensions_service.get_extension(db, extension_id)
    preview = service_extensions_service.preview_extension(db, extension)
    events = [
        event
        for event in audit_adapter.list_events(
            db,
            entity_type="service_extension",
            entity_id=str(extension.id),
            order_by="occurred_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        if event.entity_type == "service_extension"
        and event.entity_id == str(extension.id)
    ]
    staff_labels = _load_staff_labels(
        db,
        extension=extension,
        events=events,
    )
    activity = _canonical_activity(
        db,
        extension=extension,
        events=events,
        staff_labels=staff_labels,
    )
    canonical_actions = {event.action for event in events}
    activity.extend(
        _legacy_activity(
            db,
            extension=extension,
            canonical_actions=canonical_actions,
            staff_labels=staff_labels,
        )
    )
    activity.sort(
        key=lambda item: (
            _as_utc(item.occurred_at),
            item.stable_order_key,
        ),
        reverse=True,
    )
    reversal = db.scalar(
        select(ServiceExtensionReversal).where(
            ServiceExtensionReversal.extension_id == extension.id
        )
    )
    reversal_projection: ServiceExtensionReversalSummaryProjection | None = None
    if reversal is not None:
        reversed_event = next(
            (
                event
                for event in events
                if event.action == "billing.service_extension_reversed"
            ),
            None,
        )
        reversal_projection = ServiceExtensionReversalSummaryProjection(
            reason=reversal.reason,
            reversed_by_label=(
                _actor_label(reversed_event, staff_labels)
                if reversed_event is not None
                else _legacy_actor_label(reversal.reversed_by, staff_labels)
            ),
            reversed_at_display=display_format.format_timestamp(
                reversal.reversed_at,
                db,
            ),
            inspected_count=int(reversal.inspected_count),
            restored_anchor_count=int(reversal.restored_anchor_count),
            preserved_later_anchor_count=int(reversal.preserved_later_anchor_count),
            preserved_lower_anchor_count=int(reversal.preserved_lower_anchor_count),
            preserved_terminal_count=int(reversal.preserved_terminal_count),
        )

    eligibility = service_extensions_service.transition_eligibility(extension.status)
    can_apply_or_cancel = auth is not None and has_permission(
        auth,
        db,
        service_extensions_service.APPLY_SCOPE,
    )
    can_reverse = auth is not None and has_permission(
        auth,
        db,
        service_extensions_service.REVERSE_SCOPE,
    )
    status_presentation = _STATUS_PRESENTATIONS[extension.status]
    outage_window = (
        f"{display_format.format_timestamp(extension.window_start, db)} — "
        f"{display_format.format_timestamp(extension.window_end, db)}"
    )
    return ServiceExtensionDetailProjection(
        summary=ServiceExtensionSummaryProjection(
            id=extension.id,
            reason=extension.reason,
            status_presentation=status_presentation,
            days=int(extension.days),
            scope_label=_SCOPE_LABELS[extension.scope_type],
            outage_window_display=outage_window,
            created_by_label=_created_actor_label(
                extension,
                events=events,
                staff_labels=staff_labels,
            ),
            created_at_display=display_format.format_timestamp(
                extension.created_at,
                db,
            ),
        ),
        impact=_impact_projection(
            db,
            extension=extension,
            preview=preview,
        ),
        selected_customers=tuple(
            ServiceExtensionCustomerItem(
                label=item.label,
                account_number=item.account_number,
                email=item.email,
            )
            for item in preview.selected_subscribers
        ),
        sample_subscriptions=tuple(
            ServiceExtensionSubscriptionItem(
                subscriber_label=item.subscriber_label,
                login=item.login,
                next_billing_at_display=display_format.format_timestamp(
                    item.next_billing_at,
                    db,
                    fmt="%Y-%m-%d",
                ),
                service_status_label=row.subscription.status.value.title(),
                # A pending extension only *requests* restoration; the lock
                # writer remains access.subscription_lifecycle.
                restoration_pending=(
                    extension.status == ServiceExtensionStatus.pending
                    and row.subscription.status == SubscriptionStatus.suspended
                ),
                previous_billing_display=display_format.format_timestamp(
                    row.previous_next_billing_at,
                    db,
                ),
                grant_starts_display=(
                    display_format.format_timestamp(row.grant_starts_at, db)
                    if row.grant_starts_at is not None
                    else "Skipped"
                ),
                grant_ends_display=display_format.format_timestamp(
                    row.grant_ends_at,
                    db,
                ),
                anchor_basis_label=(
                    row.anchor_basis.value.replace("_", " ").title()
                    if row.anchor_basis is not None
                    else "No billing date"
                ),
            )
            for item, row in zip(
                preview.subscriptions,
                preview.interval_sample,
                strict=True,
            )
        ),
        activity=tuple(activity),
        reversal=reversal_projection,
        can_apply=eligibility.can_apply and can_apply_or_cancel,
        can_cancel=eligibility.can_cancel and can_apply_or_cancel,
        can_reverse=eligibility.can_reverse and can_reverse,
        apply_idempotency_key=(
            service_extensions_service.transition_idempotency_key(
                extension.id,
                "apply",
            )
        ),
        cancel_idempotency_key=(
            service_extensions_service.transition_idempotency_key(
                extension.id,
                "cancel",
            )
        ),
        reverse_idempotency_key=(
            service_extensions_service.transition_idempotency_key(
                extension.id,
                "reverse",
            )
        ),
    )


def build_service_extension_reversal_confirmation(
    db: Session,
    *,
    extension_id: UUID,
    reason: str,
) -> ServiceExtensionReversalConfirmationProjection:
    """Compose the destructive-action preview from the reversal owner."""

    extension = service_extensions_service.get_extension(db, extension_id)
    preview = service_extensions_service.preview_service_extension_reversal(
        db,
        extension_id=extension.id,
        reason=reason,
    )
    return ServiceExtensionReversalConfirmationProjection(
        extension_id=extension.id,
        extension_reason=extension.reason,
        reversal_reason=preview.reason,
        days=int(extension.days),
        scope_label=_SCOPE_LABELS[extension.scope_type],
        preview=preview,
    )
