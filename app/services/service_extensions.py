"""Canonical exact service-grant intervals for outage compensation.

Each application records an immutable interval and projects next_billing_at to
its end. Capped plans keep their calendar-month allowance — validity, not data.
"""

from __future__ import annotations

import enum
import hashlib
import json
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import (
    ColumnElement,
    delete,
    func,
    select,
    table,
    update,
)
from sqlalchemy import (
    column as sql_column,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionReversal,
    ServiceExtensionReversalAnchorDisposition,
    ServiceExtensionReversalEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services import settings_spec
from app.services.account_lifecycle import (
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    stage_subscription_billing_anchor,
)
from app.services.audit import AuditEvents
from app.services.common import coerce_uuid
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.domain_errors import DomainError
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

DEFAULT_MAX_EXTENSION_DAYS = 30
MIN_EXTENSION_DAYS = 1
MAX_ALLOWED_EXTENSION_DAYS = 365
PREVIEW_SAMPLE_LIMIT = 50
APPLY_BATCH_SIZE = 500
# Postgres int4 ceiling: digit strings above this are not legacy customer IDs.
# (e.g. phone numbers) and would overflow the column comparison.
_MAX_INT4 = 2_147_483_647
_EXTENSION_ID_NAMESPACE = uuid.UUID("bf7a52db-180b-45b5-89c1-9aa235dd638b")

CREATE_SCOPE = "billing:extension:create"
APPLY_SCOPE = "billing:extension:apply"
CANCEL_SCOPE = "billing:extension:apply"
REVERSE_SCOPE = "billing:extension:reverse"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension lifecycle and exact grant intervals",
    name="create_service_extension",
)
_APPLY_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension lifecycle and exact grant intervals",
    name="apply_service_extension",
)
_CANCEL_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension lifecycle and exact grant intervals",
    name="cancel_service_extension",
)
_REVERSE_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension lifecycle and exact grant intervals",
    name="reverse_service_extension",
)
_OWNER = "financial.service_extensions"
_LIFECYCLE_CONCERN = "service-extension lifecycle and exact grant intervals"
_DUPLICATE_RECONCILIATION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="reconcile_duplicate_service_extension_entries",
)
_DUPLICATE_RECONCILIATION_SCOPE = "service_extension_duplicate_repair"


_REPAIR_ANCHOR_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension billing-anchor projection",
    name="repair_service_extension_anchor_projection",
)


@dataclass(frozen=True, slots=True)
class CreateServiceExtensionCommand:
    context: CommandContext
    reason: str
    window_start: datetime
    window_end: datetime
    days: int
    scope_type: ServiceExtensionScope
    scope_id: uuid.UUID | None = None
    subscriber_identifiers: tuple[str, ...] = ()
    subscriber_ids_resolved: bool = False


@dataclass(frozen=True, slots=True)
class ApplyServiceExtensionCommand:
    context: CommandContext
    extension_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CancelServiceExtensionCommand:
    context: CommandContext
    extension_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ReverseServiceExtensionCommand:
    context: CommandContext
    extension_id: uuid.UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class RepairServiceExtensionAnchorProjectionCommand:
    context: CommandContext
    extension_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CreateServiceExtensionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    affected_count: int
    skipped_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApplyServiceExtensionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    affected_count: int
    skipped_count: int
    resumed_count: int
    still_suspended_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class CancelServiceExtensionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    affected_count: int
    skipped_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ServiceExtensionReversalPreview:
    extension_id: uuid.UUID
    reason: str
    as_of: datetime
    inspected_count: int
    restored_anchor_count: int
    preserved_later_anchor_count: int
    preserved_lower_anchor_count: int
    preserved_terminal_count: int
    active_grant_count: int
    future_grant_count: int
    expired_grant_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReverseServiceExtensionOutcome:
    extension_id: uuid.UUID
    reversal_id: uuid.UUID
    status: ServiceExtensionStatus
    inspected_count: int
    restored_anchor_count: int
    preserved_later_anchor_count: int
    preserved_lower_anchor_count: int
    preserved_terminal_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class RepairServiceExtensionAnchorProjectionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    inspected_count: int
    repaired_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ServiceExtensionPreviewSubscription:
    id: uuid.UUID
    subscriber_id: uuid.UUID
    subscriber_label: str
    login: str | None
    next_billing_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServiceExtensionPreviewSubscriber:
    id: uuid.UUID
    label: str
    account_number: str | None
    email: str | None


@dataclass(frozen=True, slots=True)
class ServiceExtensionPreview:
    subscriptions: tuple[ServiceExtensionPreviewSubscription, ...]
    selected_subscribers: tuple[ServiceExtensionPreviewSubscriber, ...]
    interval_sample: tuple[ServiceExtensionIntervalRow, ...]
    previewed_at: datetime
    total_count: int
    extendable_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class ServiceExtensionScopeChoice:
    id: uuid.UUID
    label: str


@dataclass(frozen=True, slots=True)
class ServiceExtensionScopeOptions:
    pop_sites: tuple[ServiceExtensionScopeChoice, ...]
    nas_devices: tuple[ServiceExtensionScopeChoice, ...]
    scope_types: tuple[ServiceExtensionScope, ...]
    max_days: int


CREATABLE_SERVICE_EXTENSION_SCOPES: tuple[ServiceExtensionScope, ...] = (
    ServiceExtensionScope.pop_site,
    ServiceExtensionScope.nas_device,
    ServiceExtensionScope.subscribers,
)


@dataclass(frozen=True, slots=True)
class ServiceExtensionTransitionEligibility:
    can_apply: bool
    can_cancel: bool
    can_reverse: bool


@dataclass(frozen=True, slots=True)
class _ServiceExtensionReversalPlanRow:
    entry: ServiceExtensionEntry
    subscription: Subscription
    observed_next_billing_at: datetime | None
    resulting_next_billing_at: datetime | None
    disposition: ServiceExtensionReversalAnchorDisposition


@dataclass(frozen=True, slots=True)
class _ServiceExtensionReversalPlan:
    preview: ServiceExtensionReversalPreview
    rows: tuple[_ServiceExtensionReversalPlanRow, ...]


class ServiceExtensionError(DomainError):
    """Transport-neutral service-extension failure."""


@dataclass(frozen=True, slots=True)
class ServiceExtensionGrantInterval:
    """Exact non-cash service interval decided by the extension owner."""

    starts_at: datetime
    ends_at: datetime
    anchor_basis: ServiceExtensionAnchorBasis


@dataclass(frozen=True, slots=True)
class ServiceExtensionIntervalRow:
    """Admin projection of one proposed or applied extension interval."""

    subscription: Subscription
    previous_next_billing_at: datetime | None
    grant_starts_at: datetime | None
    grant_ends_at: datetime | None
    anchor_basis: ServiceExtensionAnchorBasis | None


class ServiceExtensionDuplicateKind(str, enum.Enum):
    """Reviewed disposition for one legacy duplicate identity."""

    exact_duplicate = "exact_duplicate"
    chained_grant = "chained_grant"
    manual_review = "manual_review"


class ChainedGrantResolution(str, enum.Enum):
    """Explicit business decision for a historically chained duplicate grant."""

    preserve_as_corrective_extension = "preserve_as_corrective_extension"


@dataclass(frozen=True, slots=True)
class LegacyServiceExtensionEntryState:
    """Pre-migration entry state used by the reviewed reconciliation."""

    entry_id: uuid.UUID
    extension_id: uuid.UUID
    subscription_id: uuid.UUID
    subscriber_id: uuid.UUID
    previous_next_billing_at: datetime | None
    new_next_billing_at: datetime | None
    created_at: datetime
    downstream_reference_count: int


@dataclass(frozen=True, slots=True)
class ServiceExtensionDuplicateGroup:
    """Deterministic classification of one duplicate entry identity."""

    extension_id: uuid.UUID
    subscription_id: uuid.UUID
    subscriber_id: uuid.UUID
    extension_reason: str
    extension_days: int
    extension_window_start: datetime
    extension_window_end: datetime
    extension_status: str
    extension_affected_count: int
    extension_skipped_count: int
    extension_applied_at: datetime | None
    subscription_next_billing_at: datetime | None
    kind: ServiceExtensionDuplicateKind
    entries: tuple[LegacyServiceExtensionEntryState, ...]
    manual_review_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceExtensionDuplicatePreview:
    """Read-only, fingerprinted production-state preview."""

    groups: tuple[ServiceExtensionDuplicateGroup, ...]
    fingerprint: str

    @property
    def exact_duplicate_count(self) -> int:
        return sum(
            item.kind is ServiceExtensionDuplicateKind.exact_duplicate
            for item in self.groups
        )

    @property
    def chained_grant_count(self) -> int:
        return sum(
            item.kind is ServiceExtensionDuplicateKind.chained_grant
            for item in self.groups
        )

    @property
    def manual_review_count(self) -> int:
        return sum(
            item.kind is ServiceExtensionDuplicateKind.manual_review
            for item in self.groups
        )


@dataclass(frozen=True, slots=True)
class ReconcileServiceExtensionDuplicatesCommand:
    """Exact reviewed command for the legacy duplicate cohort."""

    context: CommandContext
    preview_fingerprint: str
    effective_at: datetime
    chained_grant_resolution: ChainedGrantResolution


@dataclass(frozen=True, slots=True)
class ServiceExtensionDuplicateReconciliationResult:
    """Stable result of one atomic duplicate reconciliation."""

    preview_fingerprint: str
    exact_duplicates_collapsed: int
    chained_grants_preserved: int
    replayed: bool


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise ServiceExtensionError(
        code=f"financial.service_extensions.{suffix}",
        message=message,
        details=details,
    )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def resolve_extension_grant_interval(
    *,
    previous_next_billing_at: datetime,
    applied_at: datetime,
    days: int,
) -> ServiceExtensionGrantInterval:
    """Resolve the exact grant interval from authoritative inputs.

    A current or future billing anchor remains additive. A stale anchor cannot
    consume compensation in the past, so the grant begins when it is applied.
    """

    previous = _as_utc(previous_next_billing_at)
    effective_at = _as_utc(applied_at)
    if previous >= effective_at:
        starts_at = previous
        basis = ServiceExtensionAnchorBasis.existing_billing_anchor
    else:
        starts_at = effective_at
        basis = ServiceExtensionAnchorBasis.application_time
    return ServiceExtensionGrantInterval(
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=days),
        anchor_basis=basis,
    )


def _state_uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _state_datetime(value: object | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    return _as_utc(value)


def _state_int(value: object) -> int:
    if isinstance(value, int):
        return value
    return int(str(value))


def _duplicate_entry_state_rows(
    db: Session,
    *,
    lock: bool = False,
) -> tuple[dict[str, object], ...]:
    """Read only columns available before migration 417.

    The candidate image contains the post-417 ORM model while production may
    still be at revision 413. Keeping this query explicit lets the owner repair
    the legacy rows before Alembic adds the interval columns and unique index.
    """

    entries = table(
        "service_extension_entries",
        sql_column("id"),
        sql_column("extension_id"),
        sql_column("subscription_id"),
        sql_column("subscriber_id"),
        sql_column("previous_next_billing_at"),
        sql_column("new_next_billing_at"),
        sql_column("created_at"),
    )
    extensions = table(
        "service_extensions",
        sql_column("id"),
        sql_column("reason"),
        sql_column("days"),
        sql_column("window_start"),
        sql_column("window_end"),
        sql_column("status"),
        sql_column("affected_count"),
        sql_column("skipped_count"),
        sql_column("applied_at"),
    )
    subscriptions = table(
        "subscriptions",
        sql_column("id"),
        sql_column("next_billing_at"),
    )
    coverage_items = table(
        "prepaid_coverage_reconciliation_items",
        sql_column("source_service_extension_entry_id"),
    )
    duplicate_groups = (
        select(entries.c.extension_id, entries.c.subscription_id)
        .group_by(entries.c.extension_id, entries.c.subscription_id)
        .having(func.count() > 1)
        .subquery("duplicate_groups")
    )
    downstream_reference_count = (
        select(func.count())
        .select_from(coverage_items)
        .where(coverage_items.c.source_service_extension_entry_id == entries.c.id)
        .correlate(entries)
        .scalar_subquery()
    )
    statement = (
        select(
            entries.c.id.label("entry_id"),
            entries.c.extension_id,
            entries.c.subscription_id,
            entries.c.subscriber_id,
            entries.c.previous_next_billing_at,
            entries.c.new_next_billing_at,
            entries.c.created_at,
            extensions.c.reason.label("extension_reason"),
            extensions.c.days.label("extension_days"),
            extensions.c.window_start.label("extension_window_start"),
            extensions.c.window_end.label("extension_window_end"),
            extensions.c.status.label("extension_status"),
            extensions.c.affected_count.label("extension_affected_count"),
            extensions.c.skipped_count.label("extension_skipped_count"),
            extensions.c.applied_at.label("extension_applied_at"),
            subscriptions.c.next_billing_at.label("subscription_next_billing_at"),
            downstream_reference_count.label("downstream_reference_count"),
        )
        .select_from(
            entries.join(
                duplicate_groups,
                (duplicate_groups.c.extension_id == entries.c.extension_id)
                & (duplicate_groups.c.subscription_id == entries.c.subscription_id),
            )
            .join(extensions, extensions.c.id == entries.c.extension_id)
            .join(subscriptions, subscriptions.c.id == entries.c.subscription_id)
        )
        .order_by(
            entries.c.extension_id,
            entries.c.subscription_id,
            entries.c.created_at,
            entries.c.id,
        )
    )
    if lock:
        statement = statement.with_for_update(of=entries)
    rows = tuple(dict(row) for row in db.execute(statement).mappings())
    if lock and rows:
        extension_ids = {_state_uuid(row["extension_id"]) for row in rows}
        subscription_ids = {_state_uuid(row["subscription_id"]) for row in rows}
        db.execute(
            select(ServiceExtension.id)
            .where(ServiceExtension.id.in_(extension_ids))
            .with_for_update()
        )
        db.execute(
            select(Subscription.id)
            .where(Subscription.id.in_(subscription_ids))
            .with_for_update()
        )
    return rows


def _entry_interval_matches_days(
    entry: LegacyServiceExtensionEntryState,
    days: int,
) -> bool:
    previous = entry.previous_next_billing_at
    new = entry.new_next_billing_at
    return (
        previous is not None
        and new is not None
        and new - previous == timedelta(days=days)
    )


def _classify_duplicate_group(
    rows: Sequence[dict[str, object]],
) -> ServiceExtensionDuplicateGroup:
    first = rows[0]
    entries = tuple(
        LegacyServiceExtensionEntryState(
            entry_id=_state_uuid(row["entry_id"]),
            extension_id=_state_uuid(row["extension_id"]),
            subscription_id=_state_uuid(row["subscription_id"]),
            subscriber_id=_state_uuid(row["subscriber_id"]),
            previous_next_billing_at=_state_datetime(row["previous_next_billing_at"]),
            new_next_billing_at=_state_datetime(row["new_next_billing_at"]),
            created_at=_state_datetime(row["created_at"]) or _now_utc(),
            downstream_reference_count=_state_int(row["downstream_reference_count"]),
        )
        for row in rows
    )
    status = str(first["extension_status"])
    days = _state_int(first["extension_days"])
    current_anchor = _state_datetime(first["subscription_next_billing_at"])
    business_states = {
        (
            item.subscriber_id,
            item.previous_next_billing_at,
            item.new_next_billing_at,
        )
        for item in entries
    }
    kind = ServiceExtensionDuplicateKind.manual_review
    manual_reason: str | None = None

    if status != ServiceExtensionStatus.applied.value:
        manual_reason = "duplicate entries belong to a non-applied extension"
    elif any(item.downstream_reference_count for item in entries):
        manual_reason = "one or more duplicate entries have downstream references"
    elif len(business_states) == 1:
        kind = ServiceExtensionDuplicateKind.exact_duplicate
    elif (
        len(entries) == 2
        and entries[0].subscriber_id == entries[1].subscriber_id
        and entries[0].new_next_billing_at == entries[1].previous_next_billing_at
        and _entry_interval_matches_days(entries[0], days)
        and _entry_interval_matches_days(entries[1], days)
        and entries[1].new_next_billing_at is not None
        and current_anchor is not None
        and current_anchor >= entries[1].new_next_billing_at
    ):
        kind = ServiceExtensionDuplicateKind.chained_grant
    else:
        manual_reason = (
            "duplicate entry values are neither exact copies nor one supported "
            "two-interval chain"
        )

    return ServiceExtensionDuplicateGroup(
        extension_id=_state_uuid(first["extension_id"]),
        subscription_id=_state_uuid(first["subscription_id"]),
        subscriber_id=_state_uuid(first["subscriber_id"]),
        extension_reason=str(first["extension_reason"]),
        extension_days=days,
        extension_window_start=_state_datetime(first["extension_window_start"])
        or _now_utc(),
        extension_window_end=_state_datetime(first["extension_window_end"])
        or _now_utc(),
        extension_status=status,
        extension_affected_count=_state_int(first["extension_affected_count"]),
        extension_skipped_count=_state_int(first["extension_skipped_count"]),
        extension_applied_at=_state_datetime(first["extension_applied_at"]),
        subscription_next_billing_at=current_anchor,
        kind=kind,
        entries=entries,
        manual_review_reason=manual_reason,
    )


def _fingerprint_datetime(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _duplicate_preview_from_rows(
    rows: Sequence[dict[str, object]],
) -> ServiceExtensionDuplicatePreview:
    grouped: dict[tuple[uuid.UUID, uuid.UUID], list[dict[str, object]]] = {}
    for row in rows:
        key = (
            _state_uuid(row["extension_id"]),
            _state_uuid(row["subscription_id"]),
        )
        grouped.setdefault(key, []).append(row)
    groups = tuple(
        _classify_duplicate_group(grouped[key])
        for key in sorted(grouped, key=lambda item: (str(item[0]), str(item[1])))
    )
    state = [
        {
            "extension_id": str(group.extension_id),
            "subscription_id": str(group.subscription_id),
            "subscriber_id": str(group.subscriber_id),
            "extension_reason": group.extension_reason,
            "extension_days": group.extension_days,
            "extension_window_start": _fingerprint_datetime(
                group.extension_window_start
            ),
            "extension_window_end": _fingerprint_datetime(group.extension_window_end),
            "extension_status": group.extension_status,
            "extension_affected_count": group.extension_affected_count,
            "extension_skipped_count": group.extension_skipped_count,
            "extension_applied_at": _fingerprint_datetime(group.extension_applied_at),
            "subscription_next_billing_at": _fingerprint_datetime(
                group.subscription_next_billing_at
            ),
            "kind": group.kind.value,
            "manual_review_reason": group.manual_review_reason,
            "entries": [
                {
                    "entry_id": str(entry.entry_id),
                    "subscriber_id": str(entry.subscriber_id),
                    "previous_next_billing_at": _fingerprint_datetime(
                        entry.previous_next_billing_at
                    ),
                    "new_next_billing_at": _fingerprint_datetime(
                        entry.new_next_billing_at
                    ),
                    "created_at": _fingerprint_datetime(entry.created_at),
                    "downstream_reference_count": (entry.downstream_reference_count),
                }
                for entry in group.entries
            ],
        }
        for group in groups
    ]
    fingerprint = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ServiceExtensionDuplicatePreview(
        groups=groups,
        fingerprint=fingerprint,
    )


def preview_service_extension_duplicate_reconciliation(
    db: Session,
) -> ServiceExtensionDuplicatePreview:
    """Preview the complete legacy duplicate cohort without writes."""

    return _duplicate_preview_from_rows(_duplicate_entry_state_rows(db))


def _replayed_duplicate_reconciliation(
    reservation: IdempotencyKey,
    command: ReconcileServiceExtensionDuplicatesCommand,
) -> ServiceExtensionDuplicateReconciliationResult:
    parts = str(reservation.ref_id or "").split(":")
    if len(parts) != 3 or parts[0] != command.preview_fingerprint:
        _error(
            "duplicate_reconciliation_idempotency_conflict",
            "Idempotency evidence does not match this reviewed duplicate cohort.",
        )
    return ServiceExtensionDuplicateReconciliationResult(
        preview_fingerprint=parts[0],
        exact_duplicates_collapsed=int(parts[1]),
        chained_grants_preserved=int(parts[2]),
        replayed=True,
    )


def reconcile_service_extension_duplicates(
    db: Session,
    command: ReconcileServiceExtensionDuplicatesCommand,
) -> ServiceExtensionDuplicateReconciliationResult:
    """Repair one exact reviewed legacy duplicate cohort atomically.

    Exact duplicate rows are collapsed to their earliest evidence row. A
    supported chained row is preserved as a separately audited corrective
    extension, so no customer entitlement or billing anchor is reduced.
    """

    def operation() -> ServiceExtensionDuplicateReconciliationResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error(
                "duplicate_reconciliation_missing_idempotency_key",
                "A bounded idempotency key is required.",
            )
        if (
            command.chained_grant_resolution
            is not ChainedGrantResolution.preserve_as_corrective_extension
        ):
            _error(
                "duplicate_reconciliation_resolution_required",
                "The reviewed chained-grant entitlement decision is required.",
            )
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _DUPLICATE_RECONCILIATION_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            return _replayed_duplicate_reconciliation(reservation, command)

        current = _duplicate_preview_from_rows(
            _duplicate_entry_state_rows(db, lock=True)
        )
        if not secrets.compare_digest(
            current.fingerprint,
            command.preview_fingerprint.strip(),
        ):
            _error(
                "duplicate_reconciliation_stale_preview",
                "Duplicate entry evidence changed after preview; preview again.",
                current_fingerprint=current.fingerprint,
            )
        if not current.groups:
            _error(
                "duplicate_reconciliation_empty_cohort",
                "No duplicate service-extension identities remain.",
            )
        if current.manual_review_count:
            _error(
                "duplicate_reconciliation_manual_review",
                "One or more duplicate groups remain outside the approved repair.",
                manual_review_count=current.manual_review_count,
            )

        exact_count = 0
        chained_count = 0
        effective_at = _as_utc(command.effective_at)
        from app.models.audit import AuditActorType

        for group in current.groups:
            if group.kind is ServiceExtensionDuplicateKind.exact_duplicate:
                removed_ids = tuple(item.entry_id for item in group.entries[1:])
                db.execute(
                    delete(ServiceExtensionEntry).where(
                        ServiceExtensionEntry.id.in_(removed_ids)
                    )
                )
                exact_count += 1
                action = "billing.service_extension_duplicate_collapsed"
                resolution_metadata: dict[str, object] = {
                    "kept_entry_id": str(group.entries[0].entry_id),
                    "removed_entry_ids": [str(value) for value in removed_ids],
                }
            else:
                corrective_entry = group.entries[1]
                corrective_extension = ServiceExtension(
                    id=uuid.uuid4(),
                    reason=(
                        "Corrective preservation of historically granted duplicate "
                        f"interval: {command.context.reason.strip()}"
                    ),
                    window_start=group.extension_window_start,
                    window_end=group.extension_window_end,
                    days=group.extension_days,
                    scope_type=ServiceExtensionScope.subscribers,
                    scope_subscriber_ids=[str(group.subscriber_id)],
                    status=ServiceExtensionStatus.applied,
                    affected_count=1,
                    skipped_count=0,
                    created_by=command.context.actor,
                    applied_by=command.context.actor,
                    applied_at=effective_at,
                    created_at=effective_at,
                )
                db.add(corrective_extension)
                db.flush()
                db.execute(
                    update(ServiceExtensionEntry)
                    .where(ServiceExtensionEntry.id == corrective_entry.entry_id)
                    .values(extension_id=corrective_extension.id)
                )
                chained_count += 1
                action = "billing.service_extension_chained_grant_preserved"
                resolution_metadata = {
                    "corrective_entry_id": str(corrective_entry.entry_id),
                    "corrective_extension_id": str(corrective_extension.id),
                    "preserved_grant_starts_at": _fingerprint_datetime(
                        corrective_entry.previous_next_billing_at
                    ),
                    "preserved_grant_ends_at": _fingerprint_datetime(
                        corrective_entry.new_next_billing_at
                    ),
                    "billing_anchor_changed": False,
                }

            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.system,
                    actor_id=command.context.actor,
                    action=action,
                    entity_type="service_extension",
                    entity_id=str(group.extension_id),
                    metadata_={
                        "preview_fingerprint": current.fingerprint,
                        "idempotency_key": key,
                        "subscription_id": str(group.subscription_id),
                        "subscriber_id": str(group.subscriber_id),
                        "extension_days": group.extension_days,
                        "resolution": group.kind.value,
                        **resolution_metadata,
                    },
                ),
            )

        reservation = IdempotencyKey(
            scope=_DUPLICATE_RECONCILIATION_SCOPE,
            key=key,
            account_id=None,
            ref_id=f"{current.fingerprint}:{exact_count}:{chained_count}",
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error(
                "duplicate_reconciliation_idempotency_conflict",
                "Idempotency key was concurrently reserved.",
            )
        return ServiceExtensionDuplicateReconciliationResult(
            preview_fingerprint=current.fingerprint,
            exact_duplicates_collapsed=exact_count,
            chained_grants_preserved=chained_count,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_DUPLICATE_RECONCILIATION_COMMAND,
        context=command.context,
        operation=operation,
    )


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def _max_extension_days(db: Session) -> int:
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "service_extension_max_days"
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_EXTENSION_DAYS
    return max(MIN_EXTENSION_DAYS, min(MAX_ALLOWED_EXTENSION_DAYS, parsed))


def _unique_subscribers(rows: list[Subscriber]) -> list[Subscriber]:
    seen: set[uuid.UUID] = set()
    unique: list[Subscriber] = []
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        unique.append(row)
    return unique


def _find_subscriber_by_identifier(db: Session, raw_identifier: str) -> Subscriber:
    identifier = str(raw_identifier or "").strip()
    if not identifier:
        _error("blank_customer_identifier", "Customer identifier cannot be blank.")

    ambiguous_detail = (
        f"Customer identifier is ambiguous: {identifier}. "
        "Use the internal customer UUID."
    )
    matches: list[Subscriber] = []

    # 1. Internal UUID.
    parsed_uuid = _parse_uuid(identifier)
    if parsed_uuid is not None:
        subscriber = db.get(Subscriber, parsed_uuid)
        if subscriber is not None:
            return subscriber
        _error(
            "customer_not_found",
            "Customer was not found.",
            identifier=identifier,
        )

    # 2. Exact account / subscriber number (case-insensitive).
    lowered = identifier.lower()
    for column in (Subscriber.account_number, Subscriber.subscriber_number):
        matches.extend(
            db.scalars(select(Subscriber).where(func.lower(column) == lowered)).all()
        )

    # 3. Imported customer id — int4-bounded so a longer digit string (e.g. an
    #    11-digit phone number) doesn't overflow the int4 column on Postgres.
    if identifier.isdigit() and int(identifier) <= _MAX_INT4:
        matches.extend(
            db.scalars(
                select(Subscriber).where(
                    Subscriber.splynx_customer_id == int(identifier)
                )
            ).all()
        )

    matches = _unique_subscribers(matches)
    if len(matches) > 1:
        _error("ambiguous_customer_identifier", ambiguous_detail)
    if len(matches) == 1:
        return matches[0]

    # 4. Email / phone via the indexed customer-identity resolver (auto-detects
    #    type, queries customer_identity_index — no full table scan). A shared
    #    contact email (non-unique post-decoupling) resolves as ambiguous.
    resolution = resolve_customer_identity(db, identifier)
    if (
        resolution.matched
        and not resolution.ambiguous
        and resolution.subscriber_id is not None
    ):
        subscriber = db.get(Subscriber, resolution.subscriber_id)
        if subscriber is not None:
            matches.append(subscriber)

    matches = _unique_subscribers(matches)
    if len(matches) == 1:
        return matches[0]
    # No exact match: an email/phone that resolved to several customers is
    # ambiguous; anything else is simply unknown.
    if resolution.ambiguous:
        _error("ambiguous_customer_identifier", ambiguous_detail)
    _error(
        "customer_not_found",
        "Customer was not found.",
        identifier=identifier,
    )


def resolve_subscriber_identifiers(
    db: Session, subscriber_ids: list[str] | None
) -> list[uuid.UUID]:
    resolved: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw_identifier in subscriber_ids or []:
        subscriber = _find_subscriber_by_identifier(db, raw_identifier)
        if subscriber.id in seen:
            continue
        seen.add(subscriber.id)
        resolved.append(subscriber.id)
    return resolved


def _coerce_resolved_subscriber_ids(
    subscriber_ids: Sequence[str | uuid.UUID] | None,
) -> list[uuid.UUID]:
    resolved: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw_id in subscriber_ids or []:
        subscriber_id = _parse_uuid(str(raw_id))
        if subscriber_id is None:
            _error(
                "invalid_customer_identifier",
                "A customer identifier in the extension scope is invalid.",
                identifier=str(raw_id),
            )
        if subscriber_id in seen:
            continue
        seen.add(subscriber_id)
        resolved.append(subscriber_id)
    return resolved


def _validate_resolved_subscriber_ids(
    db: Session, subscriber_ids: Sequence[str | uuid.UUID] | None
) -> list[uuid.UUID]:
    resolved = _coerce_resolved_subscriber_ids(subscriber_ids)
    if not resolved:
        return []
    existing = set(
        db.scalars(select(Subscriber.id).where(Subscriber.id.in_(resolved))).all()
    )
    missing = [
        str(subscriber_id)
        for subscriber_id in resolved
        if subscriber_id not in existing
    ]
    if missing:
        _error(
            "customer_not_found",
            "A selected customer was not found.",
            identifier=missing[0],
        )
    return resolved


def _subscriber_scope_rows(
    db: Session, subscriber_ids: Sequence[str | uuid.UUID] | None
) -> list[Subscriber]:
    resolved = _coerce_resolved_subscriber_ids(subscriber_ids)
    if not resolved:
        return []
    rows = {
        row.id: row
        for row in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(resolved))
        ).all()
    }
    return [rows[subscriber_id] for subscriber_id in resolved if subscriber_id in rows]


def _scope_filters(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> list:
    # Suspended subscriptions are in scope on purpose: ops reach for an
    # extension precisely when a customer lapsed during an outage window, and
    # silently skipping them left customers extended-on-paper but offline.
    filters: list[ColumnElement[bool]] = [
        Subscription.status.in_(
            (SubscriptionStatus.active, SubscriptionStatus.suspended)
        )
    ]
    if scope_type == ServiceExtensionScope.nas_device:
        if not scope_id:
            _error("missing_scope_id", "NAS device is required.")
        filters.append(Subscription.provisioning_nas_device_id == coerce_uuid(scope_id))
    elif scope_type == ServiceExtensionScope.pop_site:
        if not scope_id:
            _error("missing_scope_id", "POP site is required.")
        filters.append(
            Subscription.provisioning_nas_device.has(
                NasDevice.pop_site_id == coerce_uuid(scope_id)
            )
        )
    elif scope_type == ServiceExtensionScope.subscribers:
        ids = (
            _coerce_resolved_subscriber_ids(subscriber_ids)
            if subscriber_ids_resolved
            else resolve_subscriber_identifiers(
                db, [str(s) for s in (subscriber_ids or [])]
            )
        )
        if not ids:
            _error(
                "empty_subscriber_scope",
                "At least one customer is required.",
            )
        filters.append(Subscription.subscriber_id.in_(ids))
    return filters


def _scope_subscription_counts(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> tuple[int, int]:
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    total = db.scalar(select(func.count(Subscription.id)).where(*filters)) or 0
    extendable = (
        db.scalar(
            select(func.count(Subscription.id)).where(
                *filters, Subscription.next_billing_at.is_not(None)
            )
        )
        or 0
    )
    return int(total), int(extendable)


def _scope_subscription_sample(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    limit: int = PREVIEW_SAMPLE_LIMIT,
    subscriber_ids_resolved: bool = False,
) -> list[Subscription]:
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.subscriber))
        .where(*filters)
        .order_by(Subscription.created_at.desc(), Subscription.id)
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def resolve_scope_subscriptions(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> list[Subscription]:
    """Active subscriptions in scope, with subscriber eagerly loaded."""
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.subscriber))
        .where(*filters)
    )
    return list(db.scalars(stmt).all())


def _iter_scope_subscriptions(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    batch_size: int = APPLY_BATCH_SIZE,
    subscriber_ids_resolved: bool = False,
):
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    offset = 0
    while True:
        ids = list(
            db.scalars(
                select(Subscription.id)
                .where(*filters)
                .order_by(Subscription.id)
                .limit(batch_size)
                .offset(offset)
                .with_for_update()
            ).all()
        )
        if not ids:
            break
        subscriptions = list(
            db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(ids))
                .order_by(Subscription.id)
                .with_for_update()
            ).all()
        )
        yield from subscriptions
        offset += len(ids)


def _validated_days(db: Session, days: int) -> int:
    max_days = _max_extension_days(db)
    if not MIN_EXTENSION_DAYS <= int(days) <= max_days:
        _error(
            "invalid_days",
            f"Days must be between {MIN_EXTENSION_DAYS} and {max_days}.",
        )
    return int(days)


def _require_command_context(
    context: CommandContext,
    *,
    expected_scope: str,
) -> str:
    if context.scope != expected_scope:
        _error(
            "invalid_scope",
            "The service-extension command scope is invalid.",
            expected_scope=expected_scope,
        )
    raw_key = str(context.idempotency_key or "").strip()
    if not raw_key:
        _error(
            "missing_idempotency_key",
            "A service-extension idempotency key is required.",
        )
    try:
        return str(uuid.UUID(raw_key))
    except ValueError:
        _error(
            "invalid_idempotency_key",
            "The service-extension idempotency key must be a UUID.",
        )


def _utc_datetime(db: Session, value: datetime) -> datetime:
    if value.tzinfo is None:
        from app.services.display_format import display_timezone

        value = value.replace(tzinfo=display_timezone(db))
    return value.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extension_id(idempotency_key: str) -> uuid.UUID:
    return uuid.uuid5(_EXTENSION_ID_NAMESPACE, idempotency_key)


def _lock_create_key(db: Session, extension_id: uuid.UUID) -> None:
    """Serialize production create replays before the deterministic PK insert."""

    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(extension_id.bytes[:8], byteorder="big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))


def _candidate_extension_id(idempotency_key: str | None) -> uuid.UUID:
    raw_key = str(idempotency_key or "").strip()
    try:
        raw_key = str(uuid.UUID(raw_key))
    except ValueError:
        pass
    return _extension_id(raw_key)


def _transition_id(extension_id: uuid.UUID, action: str) -> uuid.UUID:
    return uuid.uuid5(_EXTENSION_ID_NAMESPACE, f"{extension_id}:{action}")


def transition_idempotency_key(extension_id: uuid.UUID, action: str) -> str:
    if action not in {"apply", "cancel", "reverse"}:
        _error(
            "invalid_transition_action",
            "The service-extension transition action is invalid.",
            action=action,
        )
    return str(_transition_id(extension_id, action))


def transition_eligibility(
    status: ServiceExtensionStatus,
) -> ServiceExtensionTransitionEligibility:
    pending = status == ServiceExtensionStatus.pending
    return ServiceExtensionTransitionEligibility(
        can_apply=pending,
        can_cancel=pending,
        can_reverse=status == ServiceExtensionStatus.applied,
    )


def _actor(context: CommandContext) -> tuple[AuditActorType, str]:
    prefix, separator, identifier = context.actor.partition(":")
    actor_id = identifier if separator and identifier else context.actor
    if prefix == "api_key":
        return AuditActorType.api_key, actor_id
    if prefix == "user":
        return AuditActorType.user, actor_id
    if prefix == "service":
        return AuditActorType.service, actor_id
    return AuditActorType.system, actor_id


def _create_fingerprint(
    *,
    reason: str,
    window_start: datetime,
    window_end: datetime,
    days: int,
    scope_type: ServiceExtensionScope,
    scope_id: uuid.UUID | None,
    subscriber_ids: Sequence[uuid.UUID],
) -> str:
    payload = {
        "reason": reason,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "days": days,
        "scope_type": scope_type.value,
        "scope_id": str(scope_id) if scope_id else None,
        "subscriber_ids": sorted(str(item) for item in subscriber_ids),
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _stored_command_id(
    stored: uuid.UUID | None,
    *,
    extension_id: uuid.UUID,
    action: str,
) -> uuid.UUID:
    return stored or _transition_id(extension_id, action)


def _create_outcome(
    extension: ServiceExtension,
    *,
    replayed: bool,
) -> CreateServiceExtensionOutcome:
    command_id = _stored_command_id(
        extension.create_command_id,
        extension_id=extension.id,
        action="create",
    )
    return CreateServiceExtensionOutcome(
        extension_id=extension.id,
        status=extension.status,
        affected_count=int(extension.affected_count),
        skipped_count=int(extension.skipped_count),
        command_id=command_id,
        correlation_id=extension.create_correlation_id or command_id,
        replayed=replayed,
    )


def _apply_outcome(
    extension: ServiceExtension,
    *,
    replayed: bool,
) -> ApplyServiceExtensionOutcome:
    command_id = _stored_command_id(
        extension.apply_command_id,
        extension_id=extension.id,
        action="apply",
    )
    return ApplyServiceExtensionOutcome(
        extension_id=extension.id,
        status=extension.status,
        affected_count=int(extension.affected_count),
        skipped_count=int(extension.skipped_count),
        resumed_count=int(extension.resumed_count),
        still_suspended_count=int(extension.still_suspended_count),
        command_id=command_id,
        correlation_id=extension.apply_correlation_id or command_id,
        replayed=replayed,
    )


def _cancel_outcome(
    extension: ServiceExtension,
    *,
    replayed: bool,
) -> CancelServiceExtensionOutcome:
    command_id = _stored_command_id(
        extension.cancel_command_id,
        extension_id=extension.id,
        action="cancel",
    )
    return CancelServiceExtensionOutcome(
        extension_id=extension.id,
        status=extension.status,
        affected_count=int(extension.affected_count),
        skipped_count=int(extension.skipped_count),
        command_id=command_id,
        correlation_id=extension.cancel_correlation_id or command_id,
        replayed=replayed,
    )


def _assert_create_replay(
    extension: ServiceExtension,
    *,
    fingerprint: str,
) -> CreateServiceExtensionOutcome:
    if extension.create_fingerprint_sha256 != fingerprint:
        _error(
            "idempotency_conflict",
            "The idempotency key was already used with different extension inputs.",
            extension_id=str(extension.id),
        )
    return _create_outcome(extension, replayed=True)


def _stage_lifecycle_evidence(
    db: Session,
    *,
    extension: ServiceExtension,
    context: CommandContext,
    action: str,
    event_type: EventType,
    occurred_at: datetime,
    previous_status: ServiceExtensionStatus | None,
    idempotency_key_sha256: str,
    command_fingerprint_sha256: str | None = None,
    extra_metadata: dict[str, object] | None = None,
) -> None:
    from app.services.audit_adapter import stage_audit_event
    from app.services.audit_helpers import resolve_actor_label_from_db
    from app.services.events import emit_event

    actor_type, actor_id = _actor(context)
    actor_label = resolve_actor_label_from_db(db, actor_id, actor_type)
    if not actor_label:
        actor_label = {
            AuditActorType.api_key: "Integration",
            AuditActorType.service: "Automated service",
            AuditActorType.system: "System",
            AuditActorType.user: "Staff member",
        }[actor_type]
    metadata: dict[str, object] = {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "idempotency_key_sha256": idempotency_key_sha256,
        "days": int(extension.days),
        "scope_type": extension.scope_type.value,
        "resulting_status": extension.status.value,
        "affected": int(extension.affected_count),
        "skipped": int(extension.skipped_count),
        "resumed": int(extension.resumed_count),
        "still_suspended": int(extension.still_suspended_count),
    }
    if previous_status is not None:
        metadata["previous_status"] = previous_status.value
    if command_fingerprint_sha256 is not None:
        metadata["command_fingerprint_sha256"] = command_fingerprint_sha256
    if extra_metadata:
        metadata.update(extra_metadata)
    stage_audit_event(
        db,
        action=action,
        entity_type="service_extension",
        entity_id=str(extension.id),
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        request_id=str(context.correlation_id),
        occurred_at=occurred_at,
        metadata=metadata,
    )
    emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "extension_id": str(extension.id),
            **metadata,
        },
        actor=context.actor,
    )


def create_service_extension(
    db: Session,
    command: CreateServiceExtensionCommand,
) -> CreateServiceExtensionOutcome:
    """Create or replay one pending extension in an owner-managed transaction."""

    replay_id = _candidate_extension_id(command.context.idempotency_key)

    def operation() -> CreateServiceExtensionOutcome:
        idempotency_key = _require_command_context(
            command.context,
            expected_scope=CREATE_SCOPE,
        )
        _lock_create_key(db, replay_id)
        reason = str(command.reason or "").strip()
        if not reason:
            _error("missing_reason", "Reason is required.")
        window_start = _utc_datetime(db, command.window_start)
        window_end = _utc_datetime(db, command.window_end)
        if window_end <= window_start:
            _error("invalid_window", "Outage end must be after its start.")
        days = _validated_days(db, command.days)

        if command.scope_type not in CREATABLE_SERVICE_EXTENSION_SCOPES:
            _error(
                "network_scope_retired",
                "Whole-network service extensions can no longer be created.",
                scope_type=command.scope_type.value,
            )

        resolved_subscriber_ids: list[uuid.UUID] = []
        if command.scope_type == ServiceExtensionScope.subscribers:
            resolver = (
                _validate_resolved_subscriber_ids
                if command.subscriber_ids_resolved
                else resolve_subscriber_identifiers
            )
            resolved_subscriber_ids = resolver(
                db,
                list(command.subscriber_identifiers),
            )
        scope_id = command.scope_id
        _scope_subscription_counts(
            db,
            command.scope_type,
            str(scope_id) if scope_id else None,
            resolved_subscriber_ids,
            subscriber_ids_resolved=(
                command.scope_type == ServiceExtensionScope.subscribers
            ),
        )
        fingerprint = _create_fingerprint(
            reason=reason,
            window_start=window_start,
            window_end=window_end,
            days=days,
            scope_type=command.scope_type,
            scope_id=scope_id,
            subscriber_ids=resolved_subscriber_ids,
        )
        existing = db.get(ServiceExtension, replay_id)
        if existing is not None:
            return _assert_create_replay(existing, fingerprint=fingerprint)

        now = _now_utc()
        extension = ServiceExtension(
            id=replay_id,
            reason=reason,
            window_start=window_start,
            window_end=window_end,
            days=days,
            scope_type=command.scope_type,
            scope_id=scope_id,
            scope_subscriber_ids=[str(item) for item in resolved_subscriber_ids]
            or None,
            status=ServiceExtensionStatus.pending,
            created_by=_actor(command.context)[1],
            created_at=now,
            create_idempotency_key_sha256=_sha256(idempotency_key),
            create_fingerprint_sha256=fingerprint,
            create_command_id=command.context.command_id,
            create_correlation_id=command.context.correlation_id,
        )
        db.add(extension)
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_created",
            event_type=EventType.service_extension_created,
            occurred_at=now,
            previous_status=None,
            idempotency_key_sha256=_sha256(idempotency_key),
            command_fingerprint_sha256=fingerprint,
        )
        return _create_outcome(extension, replayed=False)

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def get_extension(
    db: Session,
    extension_id: str | uuid.UUID,
    *,
    lock: bool = False,
) -> ServiceExtension:
    try:
        resolved_id = coerce_uuid(str(extension_id))
    except (TypeError, ValueError):
        _error("invalid_extension_id", "Service extension identifier is invalid.")
    statement = select(ServiceExtension).where(ServiceExtension.id == resolved_id)
    if lock:
        statement = statement.with_for_update()
    extension = db.scalar(statement)
    if not extension:
        _error("extension_not_found", "Service extension was not found.")
    return extension


def _subscriber_label(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "Unknown customer"
    return (
        str(subscriber.display_name or "").strip()
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or "Unknown customer"
    )


def preview_extension(
    db: Session,
    extension: ServiceExtension,
) -> ServiceExtensionPreview:
    """Typed preview: exact applied evidence, or a current proposal if pending."""
    scope_id = str(extension.scope_id) if extension.scope_id else None
    if extension.status in {
        ServiceExtensionStatus.applied,
        ServiceExtensionStatus.reversed,
    }:
        # Applied extensions report the immutable intervals actually recorded,
        # never a recomputed proposal.
        interval_sample = _applied_interval_sample(db, extension.id)
        sample = [row.subscription for row in interval_sample]
        extendable_count = extension.affected_count
        skipped_count = extension.skipped_count
        total_count = extendable_count + skipped_count
        previewed_at = extension.applied_at or _now_utc()
    else:
        total_count, extendable_count = _scope_subscription_counts(
            db,
            extension.scope_type,
            scope_id,
            extension.scope_subscriber_ids,
            subscriber_ids_resolved=extension.scope_type
            == ServiceExtensionScope.subscribers,
        )
        sample = _scope_subscription_sample(
            db,
            extension.scope_type,
            scope_id,
            extension.scope_subscriber_ids,
            subscriber_ids_resolved=extension.scope_type
            == ServiceExtensionScope.subscribers,
        )
        previewed_at = _now_utc()
        interval_sample = [
            _proposed_interval_row(
                subscription,
                applied_at=previewed_at,
                days=extension.days,
            )
            for subscription in sample
        ]
        skipped_count = total_count - extendable_count
    selected = (
        _subscriber_scope_rows(db, extension.scope_subscriber_ids)
        if extension.scope_type == ServiceExtensionScope.subscribers
        else []
    )
    return ServiceExtensionPreview(
        subscriptions=tuple(
            ServiceExtensionPreviewSubscription(
                id=item.id,
                subscriber_id=item.subscriber_id,
                subscriber_label=_subscriber_label(item.subscriber),
                login=item.login,
                next_billing_at=item.next_billing_at,
            )
            for item in sample
        ),
        selected_subscribers=tuple(
            ServiceExtensionPreviewSubscriber(
                id=item.id,
                label=_subscriber_label(item),
                account_number=item.account_number or item.subscriber_number,
                email=item.email,
            )
            for item in selected
        ),
        interval_sample=tuple(interval_sample),
        previewed_at=previewed_at,
        total_count=total_count,
        extendable_count=extendable_count,
        skipped_count=skipped_count,
    )


def _applied_interval_sample(
    db: Session, extension_id: uuid.UUID
) -> list[ServiceExtensionIntervalRow]:
    rows = db.execute(
        select(ServiceExtensionEntry, Subscription)
        .join(Subscription, Subscription.id == ServiceExtensionEntry.subscription_id)
        .options(joinedload(Subscription.subscriber))
        .where(ServiceExtensionEntry.extension_id == extension_id)
        .order_by(ServiceExtensionEntry.created_at.desc(), ServiceExtensionEntry.id)
        .limit(PREVIEW_SAMPLE_LIMIT)
    ).all()
    return [
        ServiceExtensionIntervalRow(
            subscription=subscription,
            previous_next_billing_at=entry.previous_next_billing_at,
            grant_starts_at=entry.grant_starts_at,
            grant_ends_at=entry.grant_ends_at,
            anchor_basis=entry.anchor_basis,
        )
        for entry, subscription in rows
    ]


def _proposed_interval_row(
    subscription: Subscription, *, applied_at: datetime, days: int
) -> ServiceExtensionIntervalRow:
    previous = subscription.next_billing_at
    interval = (
        resolve_extension_grant_interval(
            previous_next_billing_at=previous,
            applied_at=applied_at,
            days=days,
        )
        if previous is not None
        else None
    )
    return ServiceExtensionIntervalRow(
        subscription=subscription,
        previous_next_billing_at=previous,
        grant_starts_at=interval.starts_at if interval else None,
        grant_ends_at=interval.ends_at if interval else None,
        anchor_basis=interval.anchor_basis if interval else None,
    )


def cancel_service_extension(
    db: Session,
    command: CancelServiceExtensionCommand,
) -> CancelServiceExtensionOutcome:
    """Cancel or replay one pending extension without altering apply evidence."""

    def operation() -> CancelServiceExtensionOutcome:
        idempotency_key = _require_command_context(
            command.context,
            expected_scope=CANCEL_SCOPE,
        )
        extension = get_extension(db, command.extension_id, lock=True)
        if extension.status == ServiceExtensionStatus.canceled:
            return _cancel_outcome(extension, replayed=True)
        if extension.status != ServiceExtensionStatus.pending:
            _error(
                "transition_conflict",
                "Only a pending service extension can be canceled.",
                current_status=extension.status.value,
            )
        previous_status = extension.status
        now = _now_utc()
        extension.status = ServiceExtensionStatus.canceled
        extension.canceled_by = _actor(command.context)[1]
        extension.canceled_at = now
        extension.cancel_idempotency_key_sha256 = _sha256(idempotency_key)
        extension.cancel_command_id = command.context.command_id
        extension.cancel_correlation_id = command.context.correlation_id
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_canceled",
            event_type=EventType.service_extension_canceled,
            occurred_at=now,
            previous_status=previous_status,
            idempotency_key_sha256=_sha256(idempotency_key),
        )
        return _cancel_outcome(extension, replayed=False)

    return execute_owner_command(
        db,
        definition=_CANCEL_COMMAND,
        context=command.context,
        operation=operation,
    )


def _normalize_reversal_reason(reason: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        _error("missing_reversal_reason", "A reviewed reversal reason is required.")
    if len(normalized) > 2000:
        _error(
            "reversal_reason_too_long",
            "The reversal reason must not exceed 2,000 characters.",
        )
    return normalized


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _as_utc(left) == _as_utc(right)


def _service_extension_reversal_plan(
    db: Session,
    *,
    extension: ServiceExtension,
    reason: str,
    lock: bool,
) -> _ServiceExtensionReversalPlan:
    normalized_reason = _normalize_reversal_reason(reason)
    if extension.status != ServiceExtensionStatus.applied:
        _error(
            "transition_conflict",
            "Only an applied service extension can be reversed.",
            current_status=extension.status.value,
        )
    entry_statement = (
        select(ServiceExtensionEntry)
        .where(ServiceExtensionEntry.extension_id == extension.id)
        .order_by(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.id,
        )
    )
    if lock:
        entry_statement = entry_statement.with_for_update()
    entries = list(db.scalars(entry_statement).all())
    if len(entries) != int(extension.affected_count):
        _error(
            "reversal_evidence_incomplete",
            "The applied extension entry evidence does not match its outcome count.",
            expected_count=int(extension.affected_count),
            actual_count=len(entries),
        )
    subscription_ids = sorted(
        {entry.subscription_id for entry in entries},
        key=str,
    )
    subscription_statement = (
        select(Subscription)
        .where(Subscription.id.in_(subscription_ids))
        .order_by(Subscription.id)
    )
    if lock:
        subscription_statement = subscription_statement.with_for_update()
    subscriptions = {
        subscription.id: subscription
        for subscription in db.scalars(subscription_statement).all()
    }
    if len(subscriptions) != len(subscription_ids):
        _error(
            "reversal_evidence_incomplete",
            "One or more affected subscriptions no longer exist.",
            expected_count=len(subscription_ids),
            actual_count=len(subscriptions),
        )

    terminal_statuses = {
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
        SubscriptionStatus.canceled,
    }
    rows: list[_ServiceExtensionReversalPlanRow] = []
    active_grants = 0
    future_grants = 0
    expired_grants = 0
    observed_at = _now_utc()
    for entry in entries:
        subscription = subscriptions[entry.subscription_id]
        current = subscription.next_billing_at
        target = entry.previous_next_billing_at
        if subscription.status in terminal_statuses:
            disposition = ServiceExtensionReversalAnchorDisposition.preserved_terminal_subscription
            resulting = current
        elif _same_datetime(current, entry.new_next_billing_at):
            disposition = (
                ServiceExtensionReversalAnchorDisposition.restored_previous_anchor
            )
            resulting = target
        elif (
            current is not None
            and entry.new_next_billing_at is not None
            and _as_utc(current) > _as_utc(entry.new_next_billing_at)
        ):
            disposition = (
                ServiceExtensionReversalAnchorDisposition.preserved_later_anchor
            )
            resulting = current
        else:
            disposition = (
                ServiceExtensionReversalAnchorDisposition.preserved_lower_anchor
            )
            resulting = current
        rows.append(
            _ServiceExtensionReversalPlanRow(
                entry=entry,
                subscription=subscription,
                observed_next_billing_at=current,
                resulting_next_billing_at=resulting,
                disposition=disposition,
            )
        )
        starts_at = entry.grant_starts_at
        ends_at = entry.grant_ends_at
        if starts_at is not None and ends_at is not None:
            if _as_utc(ends_at) <= observed_at:
                expired_grants += 1
            elif _as_utc(starts_at) > observed_at:
                future_grants += 1
            else:
                active_grants += 1

    material = {
        "kind": "service_extension_reversal",
        "extension_id": str(extension.id),
        "status": extension.status.value,
        "reason": normalized_reason,
        "rows": [
            {
                "entry_id": str(row.entry.id),
                "subscription_id": str(row.entry.subscription_id),
                "subscription_status": row.subscription.status.value,
                "entry_previous_next_billing_at": _fingerprint_datetime(
                    row.entry.previous_next_billing_at
                ),
                "entry_new_next_billing_at": _fingerprint_datetime(
                    row.entry.new_next_billing_at
                ),
                "grant_starts_at": _fingerprint_datetime(row.entry.grant_starts_at),
                "grant_ends_at": _fingerprint_datetime(row.entry.grant_ends_at),
                "observed_next_billing_at": _fingerprint_datetime(
                    row.observed_next_billing_at
                ),
                "resulting_next_billing_at": _fingerprint_datetime(
                    row.resulting_next_billing_at
                ),
                "disposition": row.disposition.value,
            }
            for row in rows
        ],
    }
    fingerprint = _sha256(json.dumps(material, sort_keys=True, separators=(",", ":")))
    counts = {
        disposition: sum(1 for row in rows if row.disposition is disposition)
        for disposition in ServiceExtensionReversalAnchorDisposition
    }
    return _ServiceExtensionReversalPlan(
        preview=ServiceExtensionReversalPreview(
            extension_id=extension.id,
            reason=normalized_reason,
            as_of=observed_at,
            inspected_count=len(rows),
            restored_anchor_count=counts[
                ServiceExtensionReversalAnchorDisposition.restored_previous_anchor
            ],
            preserved_later_anchor_count=counts[
                ServiceExtensionReversalAnchorDisposition.preserved_later_anchor
            ],
            preserved_lower_anchor_count=counts[
                ServiceExtensionReversalAnchorDisposition.preserved_lower_anchor
            ],
            preserved_terminal_count=counts[
                ServiceExtensionReversalAnchorDisposition.preserved_terminal_subscription
            ],
            active_grant_count=active_grants,
            future_grant_count=future_grants,
            expired_grant_count=expired_grants,
            fingerprint=fingerprint,
        ),
        rows=tuple(rows),
    )


def preview_service_extension_reversal(
    db: Session,
    *,
    extension_id: uuid.UUID,
    reason: str,
) -> ServiceExtensionReversalPreview:
    """Preview the exact append-only correction without changing state."""

    extension = get_extension(db, extension_id)
    return _service_extension_reversal_plan(
        db,
        extension=extension,
        reason=reason,
        lock=False,
    ).preview


def _reversal_outcome(
    extension: ServiceExtension,
    reversal: ServiceExtensionReversal,
    *,
    replayed: bool,
) -> ReverseServiceExtensionOutcome:
    return ReverseServiceExtensionOutcome(
        extension_id=extension.id,
        reversal_id=reversal.id,
        status=extension.status,
        inspected_count=int(reversal.inspected_count),
        restored_anchor_count=int(reversal.restored_anchor_count),
        preserved_later_anchor_count=int(reversal.preserved_later_anchor_count),
        preserved_lower_anchor_count=int(reversal.preserved_lower_anchor_count),
        preserved_terminal_count=int(reversal.preserved_terminal_count),
        command_id=reversal.command_id,
        correlation_id=reversal.correlation_id,
        replayed=replayed,
    )


def reverse_service_extension(
    db: Session,
    command: ReverseServiceExtensionCommand,
) -> ReverseServiceExtensionOutcome:
    """Reverse one applied grant while preserving its immutable history."""

    def operation() -> ReverseServiceExtensionOutcome:
        idempotency_key = _require_command_context(
            command.context,
            expected_scope=REVERSE_SCOPE,
        )
        fingerprint = str(command.preview_fingerprint or "").strip().lower()
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            _error(
                "invalid_reversal_preview",
                "A valid service-extension reversal preview is required.",
            )
        extension = get_extension(db, command.extension_id, lock=True)
        existing = db.scalar(
            select(ServiceExtensionReversal).where(
                ServiceExtensionReversal.extension_id == extension.id
            )
        )
        if extension.status == ServiceExtensionStatus.reversed:
            if existing is None:
                _error(
                    "reversal_evidence_incomplete",
                    "The reversed extension has no linked reversal evidence.",
                )
            if (
                existing.preview_fingerprint_sha256 != fingerprint
                or existing.idempotency_key_sha256 != _sha256(idempotency_key)
            ):
                _error(
                    "idempotency_conflict",
                    "The extension was reversed with different confirmation evidence.",
                )
            return _reversal_outcome(extension, existing, replayed=True)
        if existing is not None:
            _error(
                "reversal_evidence_conflict",
                "Reversal evidence already exists for a non-reversed extension.",
            )
        plan = _service_extension_reversal_plan(
            db,
            extension=extension,
            reason=command.context.reason,
            lock=True,
        )
        if plan.preview.fingerprint != fingerprint:
            _error(
                "stale_reversal_preview",
                "The extension impact changed after preview; review it again.",
            )

        now = _now_utc()
        reversal_id = _transition_id(extension.id, "reverse")
        actor_id = _actor(command.context)[1]
        reversal = ServiceExtensionReversal(
            id=reversal_id,
            extension_id=extension.id,
            reason=plan.preview.reason,
            preview_fingerprint_sha256=plan.preview.fingerprint,
            idempotency_key_sha256=_sha256(idempotency_key),
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
            reversed_by=actor_id,
            reversed_at=now,
            inspected_count=plan.preview.inspected_count,
            restored_anchor_count=plan.preview.restored_anchor_count,
            preserved_later_anchor_count=(plan.preview.preserved_later_anchor_count),
            preserved_lower_anchor_count=(plan.preview.preserved_lower_anchor_count),
            preserved_terminal_count=plan.preview.preserved_terminal_count,
        )
        db.add(reversal)
        for row in plan.rows:
            if (
                row.disposition
                is ServiceExtensionReversalAnchorDisposition.restored_previous_anchor
            ):
                target = row.resulting_next_billing_at
                if target is None:
                    _error(
                        "reversal_evidence_incomplete",
                        "A restored anchor disposition requires an exact target.",
                    )
                stage_subscription_billing_anchor(
                    db,
                    row.subscription,
                    BillingAnchorProjectionCommand(
                        subscription_id=row.subscription.id,
                        expected_previous=row.subscription.next_billing_at,
                        target=_as_utc(target),
                        source=BillingAnchorProjectionSource.service_extension,
                        evidence_ref=f"service-extension-reversal:{reversal_id}",
                    ),
                )
            db.add(
                ServiceExtensionReversalEntry(
                    id=uuid.uuid5(
                        _EXTENSION_ID_NAMESPACE,
                        f"{reversal_id}:{row.entry.id}",
                    ),
                    reversal_id=reversal_id,
                    extension_entry_id=row.entry.id,
                    subscription_id=row.entry.subscription_id,
                    observed_next_billing_at=row.observed_next_billing_at,
                    resulting_next_billing_at=row.resulting_next_billing_at,
                    disposition=row.disposition,
                    created_at=now,
                )
            )
        previous_status = extension.status
        extension.status = ServiceExtensionStatus.reversed
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_reversed",
            event_type=EventType.service_extension_reversed,
            occurred_at=now,
            previous_status=previous_status,
            idempotency_key_sha256=_sha256(idempotency_key),
            command_fingerprint_sha256=plan.preview.fingerprint,
            extra_metadata={
                "reversal_id": str(reversal.id),
                "inspected": plan.preview.inspected_count,
                "anchors_restored": plan.preview.restored_anchor_count,
                "later_anchors_preserved": (plan.preview.preserved_later_anchor_count),
                "lower_anchors_preserved": (plan.preview.preserved_lower_anchor_count),
                "terminal_subscriptions_preserved": (
                    plan.preview.preserved_terminal_count
                ),
                "access_consequence": ("normal_financial_policy_reevaluation_required"),
            },
        )
        return _reversal_outcome(extension, reversal, replayed=False)

    return execute_owner_command(
        db,
        definition=_REVERSE_COMMAND,
        context=command.context,
        operation=operation,
    )


def _resume_billing_suspension(
    db: Session, subscription: Subscription, extension: ServiceExtension
) -> bool:
    """Lift billing-driven suspensions so the extension actually restores service.

    Only ``overdue`` (dunning) and ``prepaid`` (balance-lapse) locks are
    resolved; admin, fraud, FUP, and customer-hold locks are deliberately left
    in place — an outage-compensation extension must not override those.
    Returns True if the subscription came back to active.
    """
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import restore_subscription

    for reason in (EnforcementReason.overdue, EnforcementReason.prepaid):
        try:
            restore_subscription(
                db,
                str(subscription.id),
                trigger="admin",
                resolved_by=f"service_extension:{extension.id}",
                reason=reason,
                notes=f"Service extension +{extension.days}d: {extension.reason}",
            )
        except ValueError:
            _error(
                "access_restoration_failed",
                "The extension could not safely request access restoration.",
                subscription_id=str(subscription.id),
                reason=reason.value,
            )
        if subscription.status == SubscriptionStatus.active:
            return True
    return False


def apply_service_extension(
    db: Session,
    command: ApplyServiceExtensionCommand,
) -> ApplyServiceExtensionOutcome:
    """Apply or replay the locked extension transition atomically."""

    def operation() -> ApplyServiceExtensionOutcome:
        from app.services.events import emit_event

        idempotency_key = _require_command_context(
            command.context,
            expected_scope=APPLY_SCOPE,
        )
        extension = get_extension(db, command.extension_id, lock=True)
        if extension.status == ServiceExtensionStatus.applied:
            return _apply_outcome(extension, replayed=True)
        if extension.status != ServiceExtensionStatus.pending:
            _error(
                "transition_conflict",
                "Only a pending service extension can be applied.",
                current_status=extension.status.value,
            )

        # Two-person control: a service extension grants free service, so the
        # staff member who applies (approves) it must be a different person than
        # the one who created it. Enforced against the immutable created_by actor
        # recorded at creation, failing closed on a match. Only human (user)
        # approvers are gated — automated service/api-key flows (e.g. the
        # duplicate-reconciliation corrective path) are not the two-person fraud
        # vector and are exempt.
        approver_type, approver_id = _actor(command.context)
        if (
            approver_type == AuditActorType.user
            and extension.created_by
            and extension.created_by == approver_id
        ):
            _error(
                "self_approval_forbidden",
                "A service extension must be approved by a different staff member "
                "than the one who created it.",
                extension_id=str(extension.id),
                created_by=extension.created_by,
            )

        previous_status = extension.status
        now = _now_utc()
        applied = 0
        skipped = 0
        resumed = 0
        still_suspended = 0
        processed = 0
        for subscription in _iter_scope_subscriptions(
            db,
            extension.scope_type,
            str(extension.scope_id) if extension.scope_id else None,
            extension.scope_subscriber_ids,
            subscriber_ids_resolved=(
                extension.scope_type == ServiceExtensionScope.subscribers
            ),
        ):
            previous = subscription.next_billing_at
            if previous is None:
                skipped += 1
                processed += 1
                if processed % APPLY_BATCH_SIZE == 0:
                    db.flush()
                continue
            interval = resolve_extension_grant_interval(
                previous_next_billing_at=previous,
                applied_at=now,
                days=extension.days,
            )
            stage_subscription_billing_anchor(
                db,
                subscription,
                BillingAnchorProjectionCommand(
                    subscription_id=subscription.id,
                    expected_previous=previous,
                    target=interval.ends_at,
                    source=BillingAnchorProjectionSource.service_extension,
                    evidence_ref=f"service-extension:{extension.id}",
                ),
            )
            db.add(
                ServiceExtensionEntry(
                    extension_id=extension.id,
                    subscription_id=subscription.id,
                    subscriber_id=subscription.subscriber_id,
                    previous_next_billing_at=previous,
                    grant_starts_at=interval.starts_at,
                    grant_ends_at=interval.ends_at,
                    anchor_basis=interval.anchor_basis,
                    new_next_billing_at=interval.ends_at,
                    created_at=now,
                )
            )
            if subscription.status == SubscriptionStatus.suspended:
                if _resume_billing_suspension(db, subscription, extension):
                    resumed += 1
                else:
                    still_suspended += 1
            emit_event(
                db,
                EventType.service_extended,
                {
                    "schema_version": 1,
                    "extension_id": str(extension.id),
                    "subscription_id": str(subscription.id),
                    "account_id": str(subscription.subscriber_id),
                    "days": extension.days,
                    "reason": extension.reason,
                    "extended_until": subscription.next_billing_at.isoformat(),
                    "command_id": str(command.context.command_id),
                    "correlation_id": str(command.context.correlation_id),
                },
                actor=command.context.actor,
                subscription_id=subscription.id,
                subscriber_id=subscription.subscriber_id,
                account_id=subscription.subscriber_id,
            )
            applied += 1
            processed += 1
            if processed % APPLY_BATCH_SIZE == 0:
                db.flush()

        extension.status = ServiceExtensionStatus.applied
        extension.affected_count = applied
        extension.skipped_count = skipped
        extension.resumed_count = resumed
        extension.still_suspended_count = still_suspended
        extension.applied_by = _actor(command.context)[1]
        extension.applied_at = now
        extension.apply_idempotency_key_sha256 = _sha256(idempotency_key)
        extension.apply_command_id = command.context.command_id
        extension.apply_correlation_id = command.context.correlation_id
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_applied",
            event_type=EventType.service_extension_applied,
            occurred_at=now,
            previous_status=previous_status,
            idempotency_key_sha256=_sha256(idempotency_key),
        )
        return _apply_outcome(extension, replayed=False)

    return execute_owner_command(
        db,
        definition=_APPLY_COMMAND,
        context=command.context,
        operation=operation,
    )


def repair_service_extension_anchor_projection(
    db: Session,
    command: RepairServiceExtensionAnchorProjectionCommand,
) -> RepairServiceExtensionAnchorProjectionOutcome:
    """Advance non-terminal anchors that drifted below immutable entry evidence."""

    def operation() -> RepairServiceExtensionAnchorProjectionOutcome:
        from app.services.audit_adapter import stage_audit_event
        from app.services.audit_helpers import resolve_actor_label_from_db
        from app.services.events import emit_event

        idempotency_key = _require_command_context(
            command.context,
            expected_scope=APPLY_SCOPE,
        )
        extension = get_extension(db, command.extension_id, lock=True)
        if extension.status != ServiceExtensionStatus.applied:
            _error(
                "transition_conflict",
                "Only an applied service extension has a repairable anchor projection.",
                current_status=extension.status.value,
            )
        entries = list(
            db.scalars(
                select(ServiceExtensionEntry)
                .where(ServiceExtensionEntry.extension_id == extension.id)
                .order_by(
                    ServiceExtensionEntry.subscription_id,
                    ServiceExtensionEntry.id,
                )
                .with_for_update()
            ).all()
        )
        subscription_ids = sorted(
            {entry.subscription_id for entry in entries},
            key=str,
        )
        subscriptions = {
            subscription.id: subscription
            for subscription in db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(subscription_ids))
                .order_by(Subscription.id)
                .with_for_update()
            ).all()
        }
        repaired = 0
        for entry in entries:
            subscription = subscriptions.get(entry.subscription_id)
            target = entry.new_next_billing_at
            if (
                subscription is None
                or subscription.status
                not in (SubscriptionStatus.active, SubscriptionStatus.suspended)
                or target is None
            ):
                continue
            current = subscription.next_billing_at
            if current is not None and _as_utc(current) >= _as_utc(target):
                continue
            stage_subscription_billing_anchor(
                db,
                subscription,
                BillingAnchorProjectionCommand(
                    subscription_id=subscription.id,
                    expected_previous=current,
                    target=_as_utc(target),
                    source=BillingAnchorProjectionSource.service_extension,
                    evidence_ref=f"service-extension-entry:{entry.id}",
                ),
            )
            repaired += 1
        if repaired:
            now = _now_utc()
            db.flush()
            actor_type, actor_id = _actor(command.context)
            actor_label = resolve_actor_label_from_db(db, actor_id, actor_type)
            if not actor_label:
                actor_label = {
                    AuditActorType.api_key: "Integration",
                    AuditActorType.service: "Automated service",
                    AuditActorType.system: "System",
                    AuditActorType.user: "Staff member",
                }[actor_type]
            metadata: dict[str, object] = {
                "schema_version": 1,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "idempotency_key_sha256": _sha256(idempotency_key),
                "resulting_status": extension.status.value,
                "inspected": len(entries),
                "repaired": repaired,
            }
            stage_audit_event(
                db,
                action="billing.service_extension_anchor_repaired",
                entity_type="service_extension",
                entity_id=str(extension.id),
                actor_type=actor_type,
                actor_id=actor_id,
                actor_label=actor_label,
                request_id=str(command.context.correlation_id),
                occurred_at=now,
                metadata=metadata,
            )
            emit_event(
                db,
                EventType.service_extension_anchor_repaired,
                {
                    "schema_version": 1,
                    "extension_id": str(extension.id),
                    **metadata,
                },
                actor=command.context.actor,
            )
        return RepairServiceExtensionAnchorProjectionOutcome(
            extension_id=extension.id,
            status=extension.status,
            inspected_count=len(entries),
            repaired_count=repaired,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_REPAIR_ANCHOR_COMMAND,
        context=command.context,
        operation=operation,
    )


def extension_shield_reason(db: Session, account_id: str | uuid.UUID) -> str | None:
    """Why billing enforcement should skip this account, or None.

    An applied service extension grants its exact recorded interval regardless
    of arrears. Enforcement uses that same interval as coverage and billing;
    it does not maintain a second clock based on row creation time.
    """
    reasons = bulk_extension_shield_reasons(db, [coerce_uuid(str(account_id))])
    return next(iter(reasons.values()), None)


def bulk_extension_shield_reasons(
    db: Session, account_ids: Sequence[uuid.UUID] | set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Return in-force extension shield reasons for a cohort of accounts."""
    ids = {coerce_uuid(str(account_id)) for account_id in account_ids}
    if not ids:
        return {}
    now = _now_utc()
    rows = db.execute(
        select(
            ServiceExtensionEntry.subscriber_id,
            ServiceExtensionEntry.grant_ends_at,
            ServiceExtension.id,
        )
        .join(
            ServiceExtension, ServiceExtension.id == ServiceExtensionEntry.extension_id
        )
        .where(
            ServiceExtensionEntry.subscriber_id.in_(ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_starts_at <= now,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > now,
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for subscriber_id, grant_ends_at, extension_id in rows:
        assert grant_ends_at is not None
        reasons.setdefault(
            subscriber_id,
            "service extension "
            f"{extension_id} in force until {grant_ends_at.date().isoformat()}",
        )
    return reasons


def scope_options(db: Session) -> ServiceExtensionScopeOptions:
    """POP sites and NAS devices for the extension form's scope selectors."""
    from app.models.catalog import NasDevice
    from app.models.network_monitoring import PopSite

    return ServiceExtensionScopeOptions(
        pop_sites=tuple(
            ServiceExtensionScopeChoice(id=item.id, label=item.name)
            for item in db.scalars(select(PopSite).order_by(PopSite.name)).all()
        ),
        nas_devices=tuple(
            ServiceExtensionScopeChoice(id=item.id, label=item.name)
            for item in db.scalars(select(NasDevice).order_by(NasDevice.name)).all()
        ),
        scope_types=CREATABLE_SERVICE_EXTENSION_SCOPES,
        max_days=_max_extension_days(db),
    )


def list_extensions(
    db: Session, *, limit: int = 50, offset: int = 0
) -> list[ServiceExtension]:
    return list(
        db.scalars(
            select(ServiceExtension)
            .order_by(ServiceExtension.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
