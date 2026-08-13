"""Governed proposal and review coordinator for Network Map V2 assets."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.fiber_support import FiberSupportMount
from app.models.network import FiberSegment, FiberTerminationPoint
from app.models.network_map_asset_change import NetworkMapAssetChangeProposal
from app.schemas.network_map_asset_changes import (
    ApplyReviewedNetworkAssetChange,
    GovernedNetworkAssetType,
    NetworkAssetChangeOperation,
    NetworkAssetCoordinates,
    NetworkAssetDraft,
    NetworkAssetProposalAuditEntry,
    NetworkAssetProposalList,
    NetworkAssetProposalOutcome,
    NetworkAssetProposalStatus,
    NetworkAssetReviewDecision,
    NetworkAssetSnapshot,
    ReviewNetworkAssetProposalCommand,
    SubmitNetworkAssetProposalCommand,
)
from app.services import fiber_change_requests
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command

OWNER = "network.map_asset_change_governance"
PROPOSE_PERMISSION = "network:fiber:write"
REVIEW_PERMISSION = "network:fiber:review"
COMMAND_CONCERN = (
    "governed Network Map V2 asset proposal lifecycle and review coordination"
)
ENTITY_TYPE = "network_map_asset_change_proposal"

_SUBMIT = OwnerCommandDefinition(
    owner=OWNER,
    concern=COMMAND_CONCERN,
    name="submit_network_map_asset_proposal",
)
_REVIEW = OwnerCommandDefinition(
    owner=OWNER,
    concern=COMMAND_CONCERN,
    name="review_network_map_asset_proposal",
)
_SUPPORT_TYPES = frozenset({"pole", "tower", "building_attachment", "other"})


class NetworkMapAssetChangeError(DomainError):
    """Stable transport-neutral refusal from the proposal coordinator."""


def _error(code: str, message: str, **details: object) -> NetworkMapAssetChangeError:
    return NetworkMapAssetChangeError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise _error(
            "idempotency_key_required",
            "An idempotency key is required for this proposal command.",
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_actor(
    command: SubmitNetworkAssetProposalCommand | ReviewNetworkAssetProposalCommand,
    *,
    scope: str,
) -> None:
    if command.context.scope != scope:
        raise _error(
            "invalid_scope",
            "The command has an invalid authorization scope.",
            expected_scope=scope,
        )
    expected_actor = f"{command.actor_type.value}:{command.actor_id}"
    if command.context.actor != expected_actor:
        raise _error(
            "invalid_actor",
            "The command actor does not match its audit provenance.",
        )
    if not command.actor_label.strip():
        raise _error("invalid_actor", "The command actor label is required.")


def _required_name(value: str | None) -> str:
    name = (value or "").strip()
    if not name:
        raise _error("name_required", "An asset name is required.", field="name")
    if len(name) > 160:
        raise _error(
            "name_too_long",
            "The asset name cannot exceed 160 characters.",
            field="name",
        )
    return name


def _optional_text(value: str | None, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip() or None
    if normalized is not None and len(normalized) > limit:
        raise _error(
            f"{field}_too_long",
            f"{field.replace('_', ' ').title()} cannot exceed {limit} characters.",
            field=field,
        )
    return normalized


def _validated_coordinates(
    value: NetworkAssetCoordinates | None,
) -> NetworkAssetCoordinates:
    if value is None:
        raise _error(
            "coordinates_required",
            "Complete latitude and longitude are required.",
            field="coordinates",
        )
    if (
        isinstance(value.latitude, bool)
        or isinstance(value.longitude, bool)
        or not math.isfinite(value.latitude)
        or not math.isfinite(value.longitude)
        or not -90 <= value.latitude <= 90
        or not -180 <= value.longitude <= 180
    ):
        raise _error(
            "invalid_coordinates",
            "Latitude must be between -90 and 90 and longitude between -180 and 180.",
            field="coordinates",
        )
    return NetworkAssetCoordinates(
        latitude=float(value.latitude),
        longitude=float(value.longitude),
    )


def _present_fields(draft: NetworkAssetDraft) -> frozenset[str]:
    return frozenset(
        field
        for field, value in (
            ("name", draft.name),
            ("code", draft.code),
            ("coordinates", draft.coordinates),
            ("notes", draft.notes),
            ("access_point_type", draft.access_point_type),
            ("placement", draft.placement),
            ("street", draft.street),
            ("city", draft.city),
            ("support_type", draft.support_type),
        )
        if value is not None
    )


def _allowed_fields(asset_type: GovernedNetworkAssetType) -> frozenset[str]:
    common = {"name", "coordinates", "notes"}
    if asset_type is GovernedNetworkAssetType.fdh_cabinet:
        return frozenset(common | {"code"})
    if asset_type is GovernedNetworkAssetType.splice_closure:
        return frozenset(common)
    if asset_type is GovernedNetworkAssetType.access_point:
        return frozenset(
            common | {"code", "access_point_type", "placement", "street", "city"}
        )
    return frozenset(common | {"code", "support_type"})


def _validate_fields(
    *, asset_type: GovernedNetworkAssetType, draft: NetworkAssetDraft
) -> None:
    unsupported = _present_fields(draft) - _allowed_fields(asset_type)
    if unsupported:
        raise _error(
            "unsupported_fields",
            "The proposal contains fields not owned by this asset type.",
            asset_type=asset_type.value,
            fields=tuple(sorted(unsupported)),
        )


def _snapshot_digest(snapshot: NetworkAssetSnapshot) -> str:
    return _digest(snapshot.to_transport())


def _canonical_snapshot(
    db: Session,
    *,
    asset_type: GovernedNetworkAssetType,
    asset_id: UUID,
    lock: bool,
) -> NetworkAssetSnapshot:
    try:
        return fiber_change_requests.get_governed_asset_snapshot(
            db,
            asset_type=asset_type,
            asset_id=asset_id,
            lock=lock,
        )
    except fiber_change_requests.GovernedFiberAssetError as exc:
        raise _error(
            "canonical_asset_unavailable",
            exc.message,
            asset_type=asset_type.value,
            asset_id=str(asset_id),
            owner_code=exc.code,
        ) from exc


def _new_snapshot(
    *, asset_type: GovernedNetworkAssetType, draft: NetworkAssetDraft
) -> NetworkAssetSnapshot:
    _validate_fields(asset_type=asset_type, draft=draft)
    name = _required_name(draft.name)
    code = _optional_text(draft.code, field="code", limit=80)
    notes = _optional_text(draft.notes, field="notes", limit=4000)
    if asset_type is GovernedNetworkAssetType.support_structure and code is None:
        raise _error(
            "code_required",
            "A support structure code is required.",
            field="code",
        )
    support_type = _optional_text(draft.support_type, field="support_type", limit=40)
    if support_type is not None and support_type not in _SUPPORT_TYPES:
        raise _error(
            "invalid_support_type",
            "Support type must be pole, tower, building attachment, or other.",
            field="support_type",
        )
    return NetworkAssetSnapshot(
        asset_type=asset_type,
        asset_id=None,
        name=name,
        code=code,
        coordinates=_validated_coordinates(draft.coordinates),
        notes=notes,
        access_point_type=_optional_text(
            draft.access_point_type, field="access_point_type", limit=60
        ),
        placement=_optional_text(draft.placement, field="placement", limit=60),
        street=_optional_text(draft.street, field="street", limit=200),
        city=_optional_text(draft.city, field="city", limit=100),
        support_type=support_type
        or (
            "pole" if asset_type is GovernedNetworkAssetType.support_structure else None
        ),
        is_active=True,
    )


def _edited_snapshot(
    *, before: NetworkAssetSnapshot, draft: NetworkAssetDraft
) -> NetworkAssetSnapshot:
    _validate_fields(asset_type=before.asset_type, draft=draft)
    if draft.coordinates is not None:
        coordinates = _validated_coordinates(draft.coordinates)
        if coordinates != before.coordinates:
            raise _error(
                "edit_cannot_move",
                "Use a movement proposal to change coordinates.",
                field="coordinates",
            )
    support_type = (
        _optional_text(draft.support_type, field="support_type", limit=40)
        if draft.support_type is not None
        else before.support_type
    )
    if support_type is not None and support_type not in _SUPPORT_TYPES:
        raise _error(
            "invalid_support_type",
            "Support type must be pole, tower, building attachment, or other.",
            field="support_type",
        )
    after = NetworkAssetSnapshot(
        asset_type=before.asset_type,
        asset_id=before.asset_id,
        name=(_required_name(draft.name) if draft.name is not None else before.name),
        code=(
            _optional_text(draft.code, field="code", limit=80)
            if draft.code is not None
            else before.code
        ),
        coordinates=before.coordinates,
        notes=(
            _optional_text(draft.notes, field="notes", limit=4000)
            if draft.notes is not None
            else before.notes
        ),
        access_point_type=(
            _optional_text(
                draft.access_point_type,
                field="access_point_type",
                limit=60,
            )
            if draft.access_point_type is not None
            else before.access_point_type
        ),
        placement=(
            _optional_text(draft.placement, field="placement", limit=60)
            if draft.placement is not None
            else before.placement
        ),
        street=(
            _optional_text(draft.street, field="street", limit=200)
            if draft.street is not None
            else before.street
        ),
        city=(
            _optional_text(draft.city, field="city", limit=100)
            if draft.city is not None
            else before.city
        ),
        support_type=support_type,
        is_active=before.is_active,
    )
    if after == before:
        raise _error("no_change", "The proposal does not change any asset value.")
    return after


def _moved_snapshot(
    *, before: NetworkAssetSnapshot, draft: NetworkAssetDraft
) -> NetworkAssetSnapshot:
    if _present_fields(draft) - {"coordinates"}:
        raise _error(
            "move_cannot_edit",
            "A movement proposal may contain coordinates only.",
        )
    coordinates = _validated_coordinates(draft.coordinates)
    if coordinates == before.coordinates:
        raise _error("no_change", "The proposed coordinates are unchanged.")
    return NetworkAssetSnapshot(
        asset_type=before.asset_type,
        asset_id=before.asset_id,
        name=before.name,
        code=before.code,
        coordinates=coordinates,
        notes=before.notes,
        access_point_type=before.access_point_type,
        placement=before.placement,
        street=before.street,
        city=before.city,
        support_type=before.support_type,
        is_active=before.is_active,
    )


def _snapshot_from_values(values: dict[str, object]) -> NetworkAssetSnapshot:
    try:
        raw_asset_id = values.get("asset_id")
        return NetworkAssetSnapshot(
            asset_type=GovernedNetworkAssetType(str(values["asset_type"])),
            asset_id=UUID(str(raw_asset_id)) if raw_asset_id else None,
            name=str(values["name"]),
            code=str(values["code"]) if values.get("code") is not None else None,
            coordinates=NetworkAssetCoordinates(
                latitude=float(str(values["latitude"])),
                longitude=float(str(values["longitude"])),
            ),
            notes=(str(values["notes"]) if values.get("notes") is not None else None),
            access_point_type=(
                str(values["access_point_type"])
                if values.get("access_point_type") is not None
                else None
            ),
            placement=(
                str(values["placement"])
                if values.get("placement") is not None
                else None
            ),
            street=(
                str(values["street"]) if values.get("street") is not None else None
            ),
            city=str(values["city"]) if values.get("city") is not None else None,
            support_type=(
                str(values["support_type"])
                if values.get("support_type") is not None
                else None
            ),
            is_active=bool(values["is_active"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            "invalid_persisted_snapshot",
            "Persisted proposal evidence is invalid.",
        ) from exc


def _audit_entries(
    db: Session, proposal_ids: tuple[UUID, ...]
) -> dict[UUID, tuple[NetworkAssetProposalAuditEntry, ...]]:
    if not proposal_ids:
        return {}
    events = tuple(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == ENTITY_TYPE,
                AuditEvent.entity_id.in_(tuple(str(item) for item in proposal_ids)),
                AuditEvent.is_active.is_(True),
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    grouped: dict[UUID, list[NetworkAssetProposalAuditEntry]] = defaultdict(list)
    for event in events:
        if event.entity_id is None:
            continue
        try:
            proposal_id = UUID(event.entity_id)
        except ValueError:
            continue
        grouped[proposal_id].append(
            NetworkAssetProposalAuditEntry(
                action=event.action,
                actor_id=event.actor_id,
                actor_label=event.actor_label,
                occurred_at=event.occurred_at,
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def _outcome(
    row: NetworkMapAssetChangeProposal,
    *,
    audit_history: tuple[NetworkAssetProposalAuditEntry, ...] = (),
) -> NetworkAssetProposalOutcome:
    before = _snapshot_from_values(row.before_values) if row.before_values else None
    return NetworkAssetProposalOutcome(
        proposal_id=row.id,
        asset_type=GovernedNetworkAssetType(row.asset_type),
        operation=NetworkAssetChangeOperation(row.operation),
        status=NetworkAssetProposalStatus(row.status),
        target_asset_id=row.target_asset_id,
        result_asset_id=row.result_asset_id,
        before=before,
        after=_snapshot_from_values(row.after_values),
        proposal_sha256=row.proposal_sha256,
        requested_by_actor_id=row.requested_by_actor_id,
        requested_by_actor_label=row.requested_by_actor_label,
        request_reason=row.request_reason,
        reviewed_by_actor_id=row.reviewed_by_actor_id,
        reviewed_by_actor_label=row.reviewed_by_actor_label,
        review_notes=row.review_notes,
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
        applied_at=row.applied_at,
        audit_history=audit_history,
    )


def _stage_proposal_audit(
    db: Session,
    *,
    action: str,
    row: NetworkMapAssetChangeProposal,
    actor_id: UUID,
    actor_type: AuditActorType,
    actor_label: str,
    request_id: UUID,
    metadata: dict[str, object],
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type=ENTITY_TYPE,
        entity_id=str(row.id),
        actor_type=actor_type,
        actor_id=str(actor_id),
        actor_label=actor_label,
        request_id=str(request_id),
        metadata={
            "owner": OWNER,
            "proposal_sha256": row.proposal_sha256,
            "asset_type": row.asset_type,
            "operation": row.operation,
            "before": row.before_values,
            "after": row.after_values,
            **metadata,
        },
    )


def _emit(
    db: Session,
    *,
    event_type: EventType,
    row: NetworkMapAssetChangeProposal,
    actor: str,
) -> None:
    emit_event(
        db,
        event_type,
        {
            "proposal_id": str(row.id),
            "asset_type": row.asset_type,
            "operation": row.operation,
            "status": row.status,
            "target_asset_id": (
                str(row.target_asset_id) if row.target_asset_id else None
            ),
            "result_asset_id": (
                str(row.result_asset_id) if row.result_asset_id else None
            ),
            "proposal_sha256": row.proposal_sha256,
        },
        actor=actor,
    )


def submit_proposal(
    db: Session,
    command: SubmitNetworkAssetProposalCommand,
) -> NetworkAssetProposalOutcome:
    try:
        return execute_owner_command(
            db,
            definition=_SUBMIT,
            context=command.context,
            operation=lambda: _submit_proposal(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "idempotency_conflict",
            "This proposal command conflicts with an existing submission.",
        ) from exc


def _submit_proposal(
    db: Session,
    command: SubmitNetworkAssetProposalCommand,
) -> NetworkAssetProposalOutcome:
    _validate_actor(command, scope=PROPOSE_PERMISSION)
    submit_key_sha256 = _hash_key(command.context.idempotency_key)
    fingerprint = _digest(
        {
            "actor_id": str(command.actor_id),
            "asset_type": command.asset_type.value,
            "operation": command.operation.value,
            "asset_id": str(command.asset_id) if command.asset_id else None,
            "proposed": {
                "name": command.proposed.name,
                "code": command.proposed.code,
                "coordinates": (
                    command.proposed.coordinates.to_transport()
                    if command.proposed.coordinates
                    else None
                ),
                "notes": command.proposed.notes,
                "access_point_type": command.proposed.access_point_type,
                "placement": command.proposed.placement,
                "street": command.proposed.street,
                "city": command.proposed.city,
                "support_type": command.proposed.support_type,
            },
            "reason": command.context.reason,
        }
    )
    existing = db.scalar(
        select(NetworkMapAssetChangeProposal).where(
            NetworkMapAssetChangeProposal.submit_key_sha256 == submit_key_sha256
        )
    )
    if existing is not None:
        if existing.submit_fingerprint_sha256 != fingerprint:
            raise _error(
                "idempotency_conflict",
                "The submission key was reused with different proposal inputs.",
            )
        return _outcome(existing)

    if command.operation is NetworkAssetChangeOperation.create:
        if command.asset_id is not None:
            raise _error(
                "invalid_target",
                "A creation proposal cannot name an existing asset.",
            )
        before = None
        after = _new_snapshot(
            asset_type=command.asset_type,
            draft=command.proposed,
        )
    else:
        if command.asset_id is None:
            raise _error(
                "invalid_target",
                "An edit or movement proposal must name its canonical asset.",
            )
        before = _canonical_snapshot(
            db,
            asset_type=command.asset_type,
            asset_id=command.asset_id,
            lock=True,
        )
        after = (
            _edited_snapshot(before=before, draft=command.proposed)
            if command.operation is NetworkAssetChangeOperation.edit
            else _moved_snapshot(before=before, draft=command.proposed)
        )

    before_values = before.to_transport() if before else None
    after_values = after.to_transport()
    proposal_sha256 = _digest(
        {
            "schema_version": 1,
            "asset_type": command.asset_type.value,
            "operation": command.operation.value,
            "target_asset_id": str(command.asset_id) if command.asset_id else None,
            "before": before_values,
            "after": after_values,
            "requested_by_actor_id": str(command.actor_id),
            "request_reason": command.context.reason,
        }
    )
    row = NetworkMapAssetChangeProposal(
        asset_type=command.asset_type.value,
        operation=command.operation.value,
        status=NetworkAssetProposalStatus.pending.value,
        target_asset_id=command.asset_id,
        before_values=before_values,
        after_values=after_values,
        source_asset_sha256=_snapshot_digest(before) if before else None,
        proposal_sha256=proposal_sha256,
        submit_key_sha256=submit_key_sha256,
        submit_fingerprint_sha256=fingerprint,
        requested_by_actor_id=command.actor_id,
        requested_by_actor_type=command.actor_type.value,
        requested_by_actor_label=command.actor_label.strip(),
        request_reason=command.context.reason,
    )
    db.add(row)
    db.flush()
    _stage_proposal_audit(
        db,
        action="network_map_asset_change.proposed",
        row=row,
        actor_id=command.actor_id,
        actor_type=command.actor_type,
        actor_label=command.actor_label,
        request_id=command.context.correlation_id,
        metadata={"status": row.status},
    )
    _emit(
        db,
        event_type=EventType.network_map_asset_change_proposed,
        row=row,
        actor=command.context.actor,
    )
    return _outcome(row)


def _movement_blockers(
    db: Session,
    *,
    asset_type: GovernedNetworkAssetType,
    asset_id: UUID,
) -> tuple[str, ...]:
    """Return explicit topology references only; coordinates are never read."""

    endpoint_ids = select(FiberTerminationPoint.id).where(
        FiberTerminationPoint.is_active.is_(True),
        FiberTerminationPoint.ref_id == asset_id,
    )
    segment_ids = tuple(
        db.scalars(
            select(FiberSegment.id)
            .where(
                FiberSegment.is_active.is_(True),
                or_(
                    FiberSegment.from_point_id.in_(endpoint_ids),
                    FiberSegment.to_point_id.in_(endpoint_ids),
                ),
            )
            .order_by(FiberSegment.id)
        )
    )
    blockers = [f"fiber_segment:{item}" for item in segment_ids]
    if asset_type is GovernedNetworkAssetType.support_structure:
        mount_ids = tuple(
            db.scalars(
                select(FiberSupportMount.id)
                .where(
                    FiberSupportMount.support_structure_id == asset_id,
                    FiberSupportMount.is_active.is_(True),
                )
                .order_by(FiberSupportMount.id)
            )
        )
        blockers.extend(f"support_mount:{item}" for item in mount_ids)
    return tuple(blockers)


def review_proposal(
    db: Session,
    command: ReviewNetworkAssetProposalCommand,
) -> NetworkAssetProposalOutcome:
    try:
        return execute_owner_command(
            db,
            definition=_REVIEW,
            context=command.context,
            operation=lambda: _review_proposal(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "idempotency_conflict",
            "This review command conflicts with an existing review.",
        ) from exc


def _review_proposal(
    db: Session,
    command: ReviewNetworkAssetProposalCommand,
) -> NetworkAssetProposalOutcome:
    _validate_actor(command, scope=REVIEW_PERMISSION)
    review_key_sha256 = _hash_key(command.context.idempotency_key)
    review_fingerprint = _digest(
        {
            "actor_id": str(command.actor_id),
            "proposal_id": str(command.proposal_id),
            "decision": command.decision.value,
            "expected_proposal_sha256": command.expected_proposal_sha256,
            "review_notes": command.review_notes.strip(),
        }
    )
    replay = db.scalar(
        select(NetworkMapAssetChangeProposal).where(
            NetworkMapAssetChangeProposal.review_key_sha256 == review_key_sha256
        )
    )
    if replay is not None:
        if (
            replay.id != command.proposal_id
            or replay.review_fingerprint_sha256 != review_fingerprint
        ):
            raise _error(
                "idempotency_conflict",
                "The review key was reused with different review inputs.",
            )
        return _outcome(replay)

    row = db.scalar(
        select(NetworkMapAssetChangeProposal)
        .where(NetworkMapAssetChangeProposal.id == command.proposal_id)
        .with_for_update()
    )
    if row is None:
        raise _error("proposal_not_found", "The asset proposal was not found.")
    if row.proposal_sha256 != command.expected_proposal_sha256:
        raise _error(
            "proposal_digest_mismatch",
            "The proposal changed after the review was opened.",
        )
    if row.status != NetworkAssetProposalStatus.pending.value:
        raise _error(
            "proposal_already_reviewed",
            "This proposal has already been reviewed.",
            status=row.status,
        )
    if row.requested_by_actor_id == command.actor_id:
        raise _error(
            "independent_review_required",
            "The proposer cannot review their own asset proposal.",
        )
    notes = command.review_notes.strip()
    if len(notes) < 3:
        raise _error(
            "review_notes_required",
            "Reviewer comments of at least three characters are required.",
            field="review_notes",
        )
    if len(notes) > 4000:
        raise _error(
            "review_notes_too_long",
            "Reviewer comments cannot exceed 4000 characters.",
            field="review_notes",
        )

    now = datetime.now(UTC)
    if command.decision is NetworkAssetReviewDecision.reject:
        row.review_key_sha256 = review_key_sha256
        row.review_fingerprint_sha256 = review_fingerprint
        row.reviewed_by_actor_id = command.actor_id
        row.reviewed_by_actor_type = command.actor_type.value
        row.reviewed_by_actor_label = command.actor_label.strip()
        row.review_notes = notes
        row.reviewed_at = now
        row.status = NetworkAssetProposalStatus.rejected.value
        _stage_proposal_audit(
            db,
            action="network_map_asset_change.rejected",
            row=row,
            actor_id=command.actor_id,
            actor_type=command.actor_type,
            actor_label=command.actor_label,
            request_id=command.context.correlation_id,
            metadata={"status": row.status, "review_notes": notes},
        )
        _emit(
            db,
            event_type=EventType.network_map_asset_change_rejected,
            row=row,
            actor=command.context.actor,
        )
        db.flush()
        return _outcome(row)

    before = _snapshot_from_values(row.before_values) if row.before_values else None
    after = _snapshot_from_values(row.after_values)
    asset_type = GovernedNetworkAssetType(row.asset_type)
    operation = NetworkAssetChangeOperation(row.operation)
    if row.target_asset_id is not None:
        current = _canonical_snapshot(
            db,
            asset_type=asset_type,
            asset_id=row.target_asset_id,
            lock=True,
        )
        if row.source_asset_sha256 != _snapshot_digest(current):
            raise _error(
                "stale_asset",
                "The canonical asset changed after this proposal was submitted.",
                asset_id=str(row.target_asset_id),
            )
    if operation is NetworkAssetChangeOperation.move:
        assert row.target_asset_id is not None
        blockers = _movement_blockers(
            db,
            asset_type=asset_type,
            asset_id=row.target_asset_id,
        )
        if blockers:
            raise _error(
                "topology_review_required",
                "Connected assets cannot move until affected topology and route geometry are reviewed.",
                blockers=blockers,
            )
    try:
        applied = fiber_change_requests.apply_governed_map_asset_change(
            db,
            change=ApplyReviewedNetworkAssetChange(
                proposal_id=row.id,
                asset_type=asset_type,
                operation=operation,
                target_asset_id=row.target_asset_id,
                before=before,
                after=after,
            ),
        )
    except fiber_change_requests.GovernedFiberAssetError as exc:
        raise _error(
            "canonical_change_refused",
            exc.message,
            owner_code=exc.code,
        ) from exc
    except IntegrityError as exc:
        raise _error(
            "canonical_change_refused",
            "The canonical asset owner refused a conflicting asset identity.",
            asset_type=asset_type.value,
        ) from exc
    row.review_key_sha256 = review_key_sha256
    row.review_fingerprint_sha256 = review_fingerprint
    row.reviewed_by_actor_id = command.actor_id
    row.reviewed_by_actor_type = command.actor_type.value
    row.reviewed_by_actor_label = command.actor_label.strip()
    row.review_notes = notes
    row.reviewed_at = now
    row.status = NetworkAssetProposalStatus.applied.value
    row.result_asset_id = applied.asset_id
    row.applied_at = now
    _stage_proposal_audit(
        db,
        action="network_map_asset_change.applied",
        row=row,
        actor_id=command.actor_id,
        actor_type=command.actor_type,
        actor_label=command.actor_label,
        request_id=command.context.correlation_id,
        metadata={
            "status": row.status,
            "review_notes": notes,
            "result_asset_id": str(applied.asset_id),
        },
    )
    stage_audit_event(
        db,
        action=f"network_asset.{operation.value}",
        entity_type=asset_type.value,
        entity_id=str(applied.asset_id),
        actor_type=command.actor_type,
        actor_id=str(command.actor_id),
        actor_label=command.actor_label,
        request_id=str(command.context.correlation_id),
        metadata={
            "owner": "network.fiber_asset_changes",
            "coordinator": OWNER,
            "proposal_id": str(row.id),
            "proposal_sha256": row.proposal_sha256,
            "before": before.to_transport() if before else None,
            "after": applied.snapshot.to_transport(),
        },
    )
    _emit(
        db,
        event_type=EventType.network_map_asset_change_applied,
        row=row,
        actor=command.context.actor,
    )
    db.flush()
    return _outcome(row)


def list_proposals(
    db: Session,
    *,
    status: NetworkAssetProposalStatus | None = None,
    limit: int = 100,
) -> NetworkAssetProposalList:
    bounded_limit = min(max(limit, 1), 200)
    filters = []
    if status is not None:
        filters.append(NetworkMapAssetChangeProposal.status == status.value)
    total = int(
        db.scalar(select(func.count(NetworkMapAssetChangeProposal.id)).where(*filters))
        or 0
    )
    rows = tuple(
        db.scalars(
            select(NetworkMapAssetChangeProposal)
            .where(*filters)
            .order_by(
                NetworkMapAssetChangeProposal.created_at.desc(),
                NetworkMapAssetChangeProposal.id.desc(),
            )
            .limit(bounded_limit)
        )
    )
    histories = _audit_entries(db, tuple(row.id for row in rows))
    return NetworkAssetProposalList(
        proposals=tuple(
            _outcome(row, audit_history=histories.get(row.id, ())) for row in rows
        ),
        total=total,
        limit=bounded_limit,
        truncated=total > bounded_limit,
    )


def get_proposal(db: Session, *, proposal_id: UUID) -> NetworkAssetProposalOutcome:
    row = db.get(NetworkMapAssetChangeProposal, proposal_id)
    if row is None:
        raise _error("proposal_not_found", "The asset proposal was not found.")
    histories = _audit_entries(db, (row.id,))
    return _outcome(row, audit_history=histories.get(row.id, ()))


__all__ = [
    "COMMAND_CONCERN",
    "ENTITY_TYPE",
    "NetworkMapAssetChangeError",
    "OWNER",
    "PROPOSE_PERMISSION",
    "REVIEW_PERMISSION",
    "get_proposal",
    "list_proposals",
    "review_proposal",
    "submit_proposal",
]
