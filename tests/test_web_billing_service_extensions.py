"""Typed admin service-extension detail and activity projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.models.audit import AuditActorType, AuditEvent
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionReversal,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.system_user import SystemUser
from app.services import service_extensions
from app.services.owner_commands import CommandContext
from app.services.web_billing_service_extensions import (
    ServiceExtensionActivityProvenance,
    build_service_extension_detail,
)
from app.web.admin.billing_extensions import templates

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_ADMIN_AUTH = {
    "principal_id": "test-admin",
    "principal_type": "system_user",
    "roles": ["admin"],
}


def _extension(
    db_session,
    *,
    status: ServiceExtensionStatus = ServiceExtensionStatus.pending,
    created_by: str | None = None,
    created_at: datetime = _NOW,
    applied_by: str | None = None,
    applied_at: datetime | None = None,
) -> ServiceExtension:
    extension = ServiceExtension(
        reason="Outage compensation",
        window_start=_NOW - timedelta(hours=4),
        window_end=_NOW - timedelta(hours=2),
        days=2,
        scope_type=ServiceExtensionScope.network,
        status=status,
        created_by=created_by,
        created_at=created_at,
        applied_by=applied_by,
        applied_at=applied_at,
        affected_count=(
            3
            if status
            in {ServiceExtensionStatus.applied, ServiceExtensionStatus.reversed}
            else 0
        ),
        skipped_count=(
            1
            if status
            in {ServiceExtensionStatus.applied, ServiceExtensionStatus.reversed}
            else 0
        ),
    )
    db_session.add(extension)
    db_session.commit()
    db_session.refresh(extension)
    return extension


def _audit(
    db_session,
    *,
    extension_id,
    action: str,
    occurred_at: datetime,
    actor_id: str | None = None,
    actor_label: str | None = None,
    metadata: dict[str, object] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        entity_type="service_extension",
        entity_id=str(extension_id),
        status_code=200,
        is_success=True,
        occurred_at=occurred_at,
        metadata_=metadata or {},
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    return event


def test_legacy_creation_and_apply_are_reconstructed_once_with_provenance(db_session):
    extension = _extension(
        db_session,
        status=ServiceExtensionStatus.applied,
        created_by=str(uuid4()),
        created_at=_NOW - timedelta(hours=2),
        applied_by=str(uuid4()),
        applied_at=_NOW - timedelta(hours=1),
    )

    detail = build_service_extension_detail(
        db_session,
        extension_id=extension.id,
        auth=_ADMIN_AUTH,
    )

    assert [item.action_label for item in detail.activity] == ["Applied", "Created"]
    assert all(
        item.provenance == ServiceExtensionActivityProvenance.legacy_reconstructed
        for item in detail.activity
    )
    assert all(item.provenance_label for item in detail.activity)
    assert all(item.actor_label == "Former staff member" for item in detail.activity)


def test_canonical_activity_deduplicates_legacy_and_uses_exact_entity_filter(
    db_session,
):
    actor_id = str(uuid4())
    extension = _extension(
        db_session,
        status=ServiceExtensionStatus.applied,
        created_by=actor_id,
        created_at=_NOW - timedelta(hours=3),
        applied_by=actor_id,
        applied_at=_NOW - timedelta(hours=1),
    )
    other = _extension(db_session)
    _audit(
        db_session,
        extension_id=extension.id,
        action="billing.service_extension_created",
        occurred_at=_NOW - timedelta(hours=3),
        actor_id=actor_id,
        actor_label="Original Operator",
    )
    _audit(
        db_session,
        extension_id=extension.id,
        action="billing.service_extension_applied",
        occurred_at=_NOW - timedelta(hours=1),
        actor_id=actor_id,
        actor_label="Original Operator",
        metadata={"affected": 3, "skipped": 1, "resumed": 1},
    )
    _audit(
        db_session,
        extension_id=other.id,
        action="billing.service_extension_canceled",
        occurred_at=_NOW,
        actor_id=actor_id,
        actor_label="Wrong Extension",
    )

    detail = build_service_extension_detail(
        db_session,
        extension_id=extension.id,
        auth=_ADMIN_AUTH,
    )

    assert [item.action_label for item in detail.activity] == ["Applied", "Created"]
    assert all(
        item.provenance == ServiceExtensionActivityProvenance.canonical
        for item in detail.activity
    )
    assert all(item.actor_label == "Original Operator" for item in detail.activity)
    assert "Wrong Extension" not in {item.actor_label for item in detail.activity}


def test_activity_order_is_deterministic_for_equal_timestamps(db_session):
    extension = _extension(db_session)
    first = _audit(
        db_session,
        extension_id=extension.id,
        action="billing.service_extension_created",
        occurred_at=_NOW,
        actor_label="Creator",
    )
    second = _audit(
        db_session,
        extension_id=extension.id,
        action="billing.service_extension_canceled",
        occurred_at=_NOW,
        actor_label="Canceler",
    )

    detail = build_service_extension_detail(
        db_session,
        extension_id=extension.id,
        auth=_ADMIN_AUTH,
    )

    expected = sorted(
        (("Created", str(first.id)), ("Canceled", str(second.id))),
        key=lambda item: item[1],
        reverse=True,
    )
    assert [item.action_label for item in detail.activity] == [
        label for label, _event_id in expected
    ]


def test_actor_label_snapshot_survives_staff_rename_and_deletion(
    db_session, subscriber
):
    actor_id = uuid4()
    actor = SystemUser(
        id=actor_id,
        first_name="Amina",
        last_name="Okafor",
        display_name="Amina Okafor",
        email=f"extension-{uuid4().hex}@example.com",
    )
    db_session.add(actor)
    subscriber_id = str(subscriber.id)
    db_session.commit()
    created = service_extensions.create_service_extension(
        db_session,
        service_extensions.CreateServiceExtensionCommand(
            context=CommandContext.system(
                actor=f"user:{actor_id}",
                scope=service_extensions.CREATE_SCOPE,
                reason="Verify actor-label snapshot",
                idempotency_key=str(uuid4()),
            ),
            reason="Actor snapshot",
            window_start=_NOW - timedelta(hours=2),
            window_end=_NOW - timedelta(hours=1),
            days=1,
            scope_type=ServiceExtensionScope.subscribers,
            subscriber_identifiers=(subscriber_id,),
            subscriber_ids_resolved=True,
        ),
    )
    actor.display_name = "Renamed Operator"
    db_session.commit()
    db_session.delete(actor)
    db_session.commit()

    detail = build_service_extension_detail(
        db_session,
        extension_id=created.extension_id,
        auth=_ADMIN_AUTH,
    )

    assert detail.summary.created_by_label == "Amina Okafor"
    assert detail.activity[0].actor_label == "Amina Okafor"


def test_canceled_legacy_record_does_not_invent_cancellation_activity(db_session):
    extension = _extension(
        db_session,
        status=ServiceExtensionStatus.canceled,
        created_by=None,
    )

    detail = build_service_extension_detail(
        db_session,
        extension_id=extension.id,
        auth=_ADMIN_AUTH,
    )

    assert detail.summary.status_presentation.label == "Canceled"
    assert [item.action_label for item in detail.activity] == ["Created"]
    assert detail.activity[0].actor_label == "Unknown staff member"
    assert str(extension.id) not in detail.activity[0].actor_label


def test_action_controls_require_lifecycle_eligibility_and_permission(db_session):
    pending = _extension(db_session)
    applied = _extension(
        db_session,
        status=ServiceExtensionStatus.applied,
        applied_at=_NOW,
    )
    read_only_auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["billing:extension:read"],
    }

    read_only = build_service_extension_detail(
        db_session,
        extension_id=pending.id,
        auth=read_only_auth,
    )
    db_session.commit()
    admin = build_service_extension_detail(
        db_session,
        extension_id=pending.id,
        auth=_ADMIN_AUTH,
    )
    db_session.commit()
    applied_read_only = build_service_extension_detail(
        db_session,
        extension_id=applied.id,
        auth=read_only_auth,
    )
    db_session.commit()
    applied_admin = build_service_extension_detail(
        db_session,
        extension_id=applied.id,
        auth=_ADMIN_AUTH,
    )

    assert read_only.can_apply is False
    assert read_only.can_cancel is False
    assert read_only.can_reverse is False
    assert admin.can_apply is True
    assert admin.can_cancel is True
    assert admin.can_reverse is False
    assert applied_read_only.can_reverse is False
    assert applied_admin.can_reverse is True


def test_reversed_detail_projects_linked_evidence_and_activity(db_session):
    actor_id = str(uuid4())
    extension = _extension(
        db_session,
        status=ServiceExtensionStatus.reversed,
        created_at=_NOW - timedelta(hours=2),
        applied_by=actor_id,
        applied_at=_NOW - timedelta(hours=1),
    )
    db_session.add(
        ServiceExtensionReversal(
            id=uuid4(),
            extension_id=extension.id,
            reason="Incorrect outage cohort",
            preview_fingerprint_sha256="a" * 64,
            idempotency_key_sha256="b" * 64,
            command_id=uuid4(),
            correlation_id=uuid4(),
            reversed_by=actor_id,
            reversed_at=_NOW,
            inspected_count=3,
            restored_anchor_count=2,
            preserved_later_anchor_count=1,
            preserved_lower_anchor_count=0,
            preserved_terminal_count=0,
        )
    )
    db_session.commit()
    _audit(
        db_session,
        extension_id=extension.id,
        action="billing.service_extension_reversed",
        occurred_at=_NOW,
        actor_id=actor_id,
        actor_label="Reversal Operator",
        metadata={
            "anchors_restored": 2,
            "later_anchors_preserved": 1,
        },
    )

    detail = build_service_extension_detail(
        db_session,
        extension_id=extension.id,
        auth=_ADMIN_AUTH,
    )

    assert detail.summary.status_presentation.label == "Reversed"
    assert detail.can_reverse is False
    assert detail.reversal is not None
    assert detail.reversal.reason == "Incorrect outage cohort"
    assert detail.reversal.reversed_by_label == "Reversal Operator"
    assert detail.reversal.restored_anchor_count == 2
    assert detail.activity[0].action_label == "Reversed"
    assert "2 billing anchor(s) restored" in detail.activity[0].details


def test_template_renders_owner_projection_without_local_decision_maps():
    source = Path("templates/admin/billing/service_extension_detail.html").read_text(
        encoding="utf-8"
    )

    assert 'card("Recent activity"' in source
    assert source.index('card("Recent activity"') < source.index(
        'card("Service grant intervals (sample)"'
    )
    assert "timeline_item(" in source
    assert 'aria-label="Service extension recent activity"' in source
    assert "flex flex-col" in source
    assert "detail.summary.status_presentation" in source
    assert "detail.can_apply" in source
    assert "detail.can_cancel" in source
    assert "detail.can_reverse" in source
    assert "Preview reversal impact" in source
    assert "status_colors" not in source
    assert "app_datetime" not in source
    assert "View all" not in source


def test_reversal_confirmation_template_compiles():
    templates.env.get_template("admin/billing/service_extension_reversal_confirm.html")


def test_create_form_preserves_server_idempotency_key_and_submit_lock():
    form = Path("templates/admin/billing/service_extension_form.html").read_text(
        encoding="utf-8"
    )
    route = Path("app/web/admin/billing_extensions.py").read_text(encoding="utf-8")

    assert 'name="idempotency_key"' in form
    assert "form_values.idempotency_key" in form
    assert '@submit="submitting = true"' in form
    assert ':disabled="submitting"' in form
    assert "_form_context(" in route
    assert "idempotency_key=idempotency_key" in route


def test_create_form_scope_contract_omits_network(db_session):
    options = service_extensions.scope_options(db_session)

    assert options.scope_types == (
        ServiceExtensionScope.pop_site,
        ServiceExtensionScope.nas_device,
        ServiceExtensionScope.subscribers,
    )
