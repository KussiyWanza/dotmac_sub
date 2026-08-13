"""Service extensions: outage validity compensation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.catalog import NasVendor, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStore
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
from app.models.subscription_engine import SettingValueType
from app.schemas.catalog import NasDeviceCreate, SubscriptionCreate
from app.services import catalog as catalog_service
from app.services import nas as nas_service
from app.services import service_extensions as svc
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_coverage import (
    PrepaidCoverageSource,
    PrepaidCoverageStatus,
    resolve_prepaid_service_coverage,
)
from app.services.service_extensions import ServiceExtensionError
from app.services.settings_cache import SettingsCache
from app.services.settings_spec import get_spec
from app.web.admin.billing_extensions import (
    _service_extension_failure_diagnostics,
    _subscriber_scope_inputs,
)

_WIN_START = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
_WIN_END = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)
_APPLIED_AT = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fixed_application_time(monkeypatch):
    # The _create/_apply/_cancel helpers below already hand the owner commands a
    # transaction-free session, so only the clock needs pinning here.
    monkeypatch.setattr(svc, "_now_utc", lambda: _APPLIED_AT)


def _naive(dt):
    return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt


def _command_context(
    scope: str,
    *,
    actor_id: str = "admin-1",
    idempotency_key: str | None = None,
    reason: str = "Service-extension test",
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{actor_id}",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key or str(uuid4()),
    )


def _create(db_session, **values):
    if db_session.in_transaction():
        db_session.commit()
    context = values.pop(
        "context",
        _command_context(
            svc.CREATE_SCOPE,
            actor_id=str(values.pop("created_by", "admin-1") or "admin-1"),
        ),
    )
    subscriber_ids = values.pop("subscriber_ids", None)
    scope_type = values.pop("scope_type")
    # Most lifecycle tests historically used network as a convenient broad
    # fixture. New commands must use an allowed scope; resolve that fixture
    # cohort explicitly unless a test is exercising historical network data.
    if scope_type == ServiceExtensionScope.network:
        scope_type = ServiceExtensionScope.subscribers
        subscriber_ids = [
            str(item) for item in db_session.scalars(select(Subscriber.id)).all()
        ]
        values["subscriber_ids_resolved"] = True
        db_session.commit()
    outcome = svc.create_service_extension(
        db_session,
        svc.CreateServiceExtensionCommand(
            context=context,
            reason=values.pop("reason"),
            window_start=values.pop("window_start"),
            window_end=values.pop("window_end"),
            days=values.pop("days"),
            scope_type=scope_type,
            scope_id=(
                UUID(str(raw_scope_id))
                if (raw_scope_id := values.pop("scope_id", None))
                else None
            ),
            subscriber_identifiers=tuple(subscriber_ids or ()),
            subscriber_ids_resolved=values.pop("subscriber_ids_resolved", False),
        ),
    )
    extension = svc.get_extension(db_session, outcome.extension_id)
    db_session.expunge(extension)
    db_session.commit()
    return extension


def _existing_network_extension(db_session, **values) -> ServiceExtension:
    """Persist a pre-retirement network aggregate for compatibility tests."""
    extension = ServiceExtension(
        reason=values.pop("reason"),
        window_start=values.pop("window_start"),
        window_end=values.pop("window_end"),
        days=values.pop("days"),
        scope_type=ServiceExtensionScope.network,
        status=ServiceExtensionStatus.pending,
        created_by=values.pop("created_by", "admin-1"),
        created_at=_APPLIED_AT,
    )
    db_session.add(extension)
    db_session.commit()
    db_session.refresh(extension)
    return extension


def _apply(db_session, extension_id, *, actor_id: str = "approver-1"):
    if db_session.in_transaction():
        db_session.commit()
    return svc.apply_service_extension(
        db_session,
        svc.ApplyServiceExtensionCommand(
            context=_command_context(svc.APPLY_SCOPE, actor_id=actor_id),
            extension_id=UUID(str(extension_id)),
        ),
    )


def _cancel(db_session, extension_id, *, actor_id: str = "admin-1"):
    if db_session.in_transaction():
        db_session.commit()
    return svc.cancel_service_extension(
        db_session,
        svc.CancelServiceExtensionCommand(
            context=_command_context(svc.CANCEL_SCOPE, actor_id=actor_id),
            extension_id=UUID(str(extension_id)),
        ),
    )


def _reverse(
    db_session,
    extension_id,
    *,
    reason: str = "Incorrect outage compensation",
    actor_id: str = "reverser-1",
):
    if db_session.in_transaction():
        db_session.commit()
    stored = db_session.scalar(
        select(ServiceExtensionReversal).where(
            ServiceExtensionReversal.extension_id == UUID(str(extension_id))
        )
    )
    fingerprint = (
        stored.preview_fingerprint_sha256
        if stored is not None
        else svc.preview_service_extension_reversal(
            db_session,
            extension_id=UUID(str(extension_id)),
            reason=reason,
        ).fingerprint
    )
    db_session.commit()
    return svc.reverse_service_extension(
        db_session,
        svc.ReverseServiceExtensionCommand(
            context=_command_context(
                svc.REVERSE_SCOPE,
                actor_id=actor_id,
                idempotency_key=svc.transition_idempotency_key(
                    UUID(str(extension_id)),
                    "reverse",
                ),
                reason=reason,
            ),
            extension_id=UUID(str(extension_id)),
            preview_fingerprint=fingerprint,
        ),
    )


def _another_subscriber(db_session):
    sub = Subscriber(
        first_name="Out", last_name="Age", email=f"ext-{uuid4().hex[:8]}@example.com"
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _sub(db_session, subscriber, catalog_offer, *, nas_id=None, next_billing_at=None):
    return catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas_id,
            next_billing_at=next_billing_at or datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )


def test_create_requires_valid_window_and_days(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    with pytest.raises(ServiceExtensionError) as exc:
        _create(
            db_session,
            reason="x",
            window_start=_WIN_END,
            window_end=_WIN_START,  # end before start
            days=2,
            scope_type=ServiceExtensionScope.network,
        )
    assert exc.value.code.endswith("invalid_window")

    with pytest.raises(ServiceExtensionError):
        _create(
            db_session,
            reason="x",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=99,  # over MAX
            scope_type=ServiceExtensionScope.network,
        )


def test_service_extension_max_days_setting(db_session, subscriber, catalog_offer):
    spec = get_spec(SettingDomain.billing, "service_extension_max_days")
    assert spec is not None
    assert spec.default == 30
    assert spec.min_value == 1
    assert spec.max_value == 365

    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="service_extension_max_days",
            value_type=SettingValueType.integer,
            value_text="3",
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(SettingDomain.billing.value, "service_extension_max_days")
    _sub(db_session, subscriber, catalog_offer)

    assert svc.scope_options(db_session).max_days == 3
    with pytest.raises(ServiceExtensionError) as exc:
        _create(
            db_session,
            reason="x",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=4,
            scope_type=ServiceExtensionScope.network,
        )

    assert exc.value.code.endswith("invalid_days")
    assert "between 1 and 3" in exc.value.message


def test_new_network_scope_is_absent_and_rejected_without_evidence(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)
    db_session.commit()

    assert (
        ServiceExtensionScope.network not in svc.scope_options(db_session).scope_types
    )
    db_session.commit()
    command = svc.CreateServiceExtensionCommand(
        context=_command_context(svc.CREATE_SCOPE),
        reason="Retired broad grant",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )

    with pytest.raises(ServiceExtensionError) as exc:
        svc.create_service_extension(db_session, command)

    assert exc.value.code.endswith("network_scope_retired")
    assert not db_session.in_transaction()
    assert db_session.query(ServiceExtension).count() == 0
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "service_extension")
        .count()
        == 0
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "billing.service_extension_created")
        .count()
        == 0
    )


def test_apply_network_scope_extends_all_active(db_session, subscriber, catalog_offer):
    s1 = _sub(
        db_session,
        subscriber,
        catalog_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    s2 = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 15, tzinfo=UTC),
    )

    ext = _existing_network_extension(
        db_session,
        reason="Backbone outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        created_by="admin-1",
    )
    applied = _apply(db_session, ext.id, actor_id="approver-1")

    assert applied.status == ServiceExtensionStatus.applied
    assert applied.affected_count == 2
    db_session.refresh(s1)
    db_session.refresh(s2)
    assert _naive(s1.next_billing_at) == datetime(2026, 7, 3)
    assert _naive(s2.next_billing_at) == datetime(2026, 7, 17)

    entries = (
        db_session.query(ServiceExtensionEntry)
        .filter(ServiceExtensionEntry.extension_id == ext.id)
        .all()
    )
    assert len(entries) == 2
    by_subscription = {entry.subscription_id: entry for entry in entries}
    assert _naive(by_subscription[s1.id].grant_starts_at) == datetime(2026, 7, 1)
    assert _naive(by_subscription[s1.id].grant_ends_at) == datetime(2026, 7, 3)
    assert (
        by_subscription[s1.id].anchor_basis
        == ServiceExtensionAnchorBasis.existing_billing_anchor
    )
    assert _naive(by_subscription[s2.id].grant_starts_at) == datetime(2026, 7, 15)
    assert _naive(by_subscription[s2.id].grant_ends_at) == datetime(2026, 7, 17)


def test_apply_rejects_self_approval_by_the_creator(
    db_session, subscriber, catalog_offer
):
    # Two-person control: a service extension grants free service, so the staff
    # member who created it must not be the one who applies (approves) it.
    _sub(db_session, subscriber, catalog_offer)
    ext = _create(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
        created_by="maker-1",
    )

    with pytest.raises(ServiceExtensionError) as exc:
        _apply(db_session, ext.id, actor_id="maker-1")
    assert exc.value.code.endswith("self_approval_forbidden")

    # The extension stays pending; a different staff member can approve it.
    still = svc.get_extension(db_session, ext.id)
    assert still.status == ServiceExtensionStatus.pending
    applied = _apply(db_session, ext.id, actor_id="checker-2")
    assert applied.status == ServiceExtensionStatus.applied


def test_stale_anchor_grants_from_application_time(
    db_session, subscriber, catalog_offer, monkeypatch
):
    applied_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(svc, "_now_utc", lambda: applied_at)
    subscription = _sub(
        db_session,
        subscriber,
        catalog_offer,
        next_billing_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    ext = _create(
        db_session,
        reason="Delayed outage compensation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=10,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[str(subscriber.id)],
    )

    preview = svc.preview_extension(db_session, ext)
    proposed = preview.interval_sample[0]
    assert proposed.grant_starts_at == applied_at
    assert proposed.grant_ends_at == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    assert proposed.anchor_basis == ServiceExtensionAnchorBasis.application_time

    applied = _apply(db_session, ext.id, actor_id="approver-1")

    db_session.refresh(subscription)
    entry = (
        db_session.query(ServiceExtensionEntry)
        .filter(ServiceExtensionEntry.extension_id == ext.id)
        .one()
    )
    assert _naive(subscription.next_billing_at) == datetime(2026, 8, 3, 12, 0)
    assert _naive(entry.previous_next_billing_at) == datetime(2026, 7, 2)
    assert _naive(entry.grant_starts_at) == datetime(2026, 7, 24, 12, 0)
    assert _naive(entry.grant_ends_at) == datetime(2026, 8, 3, 12, 0)
    assert entry.anchor_basis == ServiceExtensionAnchorBasis.application_time
    assert entry.new_next_billing_at == entry.grant_ends_at
    applied_extension = svc.get_extension(db_session, applied.extension_id)
    assert applied_extension.applied_at == entry.created_at
    coverage = resolve_prepaid_service_coverage(
        db_session,
        [subscription],
        as_of=applied_at + timedelta(days=1),
    )[subscription.id]
    assert coverage.status == PrepaidCoverageStatus.covered
    assert coverage.evidence is not None
    assert coverage.evidence.source == PrepaidCoverageSource.service_extension_grant
    assert coverage.evidence.starts_at == entry.grant_starts_at
    assert coverage.evidence.ends_at == entry.grant_ends_at
    assert svc.extension_shield_reason(db_session, subscriber.id) is not None


def test_apply_is_idempotent(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    ext = _create(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
    )
    first = _apply(db_session, ext.id)
    second = _apply(db_session, ext.id)
    assert first.replayed is False
    assert second.replayed is True


def test_sequential_extensions_compose_from_latest_grant_end(
    db_session, subscriber, catalog_offer, monkeypatch
):
    applied_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(svc, "_now_utc", lambda: applied_at)
    subscription = _sub(
        db_session,
        subscriber,
        catalog_offer,
        next_billing_at=datetime(2026, 7, 2, tzinfo=UTC),
    )

    entries: list[ServiceExtensionEntry] = []
    for reason in ("First correction", "Second correction"):
        extension = _create(
            db_session,
            reason=reason,
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=10,
            scope_type=ServiceExtensionScope.subscribers,
            subscriber_ids=[str(subscriber.id)],
        )
        _apply(db_session, extension.id, actor_id="approver-1")
        entries.append(
            db_session.query(ServiceExtensionEntry)
            .filter(ServiceExtensionEntry.extension_id == extension.id)
            .one()
        )

    db_session.refresh(subscription)
    assert _naive(subscription.next_billing_at) == datetime(2026, 8, 13, 12, 0)
    assert entries[0].anchor_basis == ServiceExtensionAnchorBasis.application_time
    assert (
        entries[1].anchor_basis == ServiceExtensionAnchorBasis.existing_billing_anchor
    )
    assert entries[1].grant_starts_at == entries[0].grant_ends_at


def test_nas_scope_only_extends_matching(db_session, subscriber, catalog_offer):
    nas = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS-A",
            vendor=NasVendor.mikrotik,
            ip_address="10.0.0.1",
            management_ip="10.0.0.1",
        ),
    )
    on_nas = _sub(
        db_session,
        subscriber,
        catalog_offer,
        nas_id=nas.id,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    off_nas = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    ext = _create(
        db_session,
        reason="NAS down",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=3,
        scope_type=ServiceExtensionScope.nas_device,
        scope_id=str(nas.id),
    )
    applied = _apply(db_session, ext.id)

    assert applied.affected_count == 1
    db_session.refresh(on_nas)
    db_session.refresh(off_nas)
    assert _naive(on_nas.next_billing_at) == datetime(2026, 7, 4)
    assert _naive(off_nas.next_billing_at) == datetime(2026, 7, 1)


def test_skips_subscription_without_billing_date(db_session, subscriber, catalog_offer):
    no_date = _sub(db_session, subscriber, catalog_offer)
    no_date.next_billing_at = None
    db_session.commit()

    ext = _create(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )
    applied = _apply(db_session, ext.id)
    assert applied.affected_count == 0
    assert applied.skipped_count == 1


def test_cancel_pending_extension(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    ext = _create(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
    )
    canceled = _cancel(db_session, ext.id, actor_id="admin-1")
    assert canceled.status == ServiceExtensionStatus.canceled
    with pytest.raises(ServiceExtensionError):
        _apply(db_session, ext.id)


def test_reverse_applied_extension_preserves_history_and_only_restores_exact_anchor(
    db_session,
    subscriber,
    catalog_offer,
):
    exact = _sub(
        db_session,
        subscriber,
        catalog_offer,
        next_billing_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    later = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    lower = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 4, tzinfo=UTC),
    )
    terminal = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 5, tzinfo=UTC),
    )
    extension = _create(
        db_session,
        reason="Incorrect network grant",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=7,
        scope_type=ServiceExtensionScope.network,
    )
    _apply(db_session, extension.id)
    entries = {
        entry.subscription_id: entry
        for entry in db_session.scalars(
            select(ServiceExtensionEntry).where(
                ServiceExtensionEntry.extension_id == extension.id
            )
        ).all()
    }
    exact_entry = entries[exact.id]
    later.next_billing_at = entries[later.id].new_next_billing_at + timedelta(days=30)
    lower.next_billing_at = entries[lower.id].new_next_billing_at - timedelta(days=1)
    terminal.status = SubscriptionStatus.canceled
    db_session.commit()

    preview = svc.preview_service_extension_reversal(
        db_session,
        extension_id=extension.id,
        reason="Grant created for the wrong incident",
    )

    assert preview.inspected_count == 4
    assert preview.restored_anchor_count == 1
    assert preview.preserved_later_anchor_count == 1
    assert preview.preserved_lower_anchor_count == 1
    assert preview.preserved_terminal_count == 1
    later_before = later.next_billing_at
    lower_before = lower.next_billing_at
    terminal_before = terminal.next_billing_at
    outcome = _reverse(
        db_session,
        extension.id,
        reason="Grant created for the wrong incident",
    )

    assert outcome.status == ServiceExtensionStatus.reversed
    assert outcome.inspected_count == 4
    db_session.refresh(exact)
    db_session.refresh(later)
    db_session.refresh(lower)
    db_session.refresh(terminal)
    assert _naive(exact.next_billing_at) == _naive(exact_entry.previous_next_billing_at)
    assert _naive(later.next_billing_at) == _naive(later_before)
    assert _naive(lower.next_billing_at) == _naive(lower_before)
    assert _naive(terminal.next_billing_at) == _naive(terminal_before)

    reversal = db_session.scalar(
        select(ServiceExtensionReversal).where(
            ServiceExtensionReversal.extension_id == extension.id
        )
    )
    assert reversal is not None
    results = list(
        db_session.scalars(
            select(ServiceExtensionReversalEntry).where(
                ServiceExtensionReversalEntry.reversal_id == reversal.id
            )
        ).all()
    )
    assert len(results) == 4
    assert {item.disposition for item in results} == {
        ServiceExtensionReversalAnchorDisposition.restored_previous_anchor,
        ServiceExtensionReversalAnchorDisposition.preserved_later_anchor,
        ServiceExtensionReversalAnchorDisposition.preserved_lower_anchor,
        ServiceExtensionReversalAnchorDisposition.preserved_terminal_subscription,
    }
    stored_entries = list(
        db_session.scalars(
            select(ServiceExtensionEntry).where(
                ServiceExtensionEntry.extension_id == extension.id
            )
        ).all()
    )
    assert len(stored_entries) == 4
    coverage = resolve_prepaid_service_coverage(
        db_session,
        [exact],
        as_of=_APPLIED_AT,
    )[exact.id]
    assert coverage.status == PrepaidCoverageStatus.unresolved_projection
    assert coverage.evidence is None
    actions = set(
        db_session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_type == "service_extension",
                AuditEvent.entity_id == str(extension.id),
            )
        ).all()
    )
    assert "billing.service_extension_reversed" in actions
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "billing.service_extension_reversed",
            EventStore.payload["extension_id"].as_string() == str(extension.id),
        )
        .count()
        == 1
    )


def test_reverse_is_idempotent_and_rejects_changed_confirmation(
    db_session,
    subscriber,
    catalog_offer,
):
    _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Mistaken grant",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )
    _apply(db_session, extension.id)
    first = _reverse(db_session, extension.id, reason="Wrong outage selected")
    second = _reverse(db_session, extension.id, reason="Wrong outage selected")

    assert first.replayed is False
    assert second.replayed is True
    assert first.reversal_id == second.reversal_id
    assert db_session.query(ServiceExtensionReversal).count() == 1
    assert db_session.query(ServiceExtensionReversalEntry).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "billing.service_extension_reversed")
        .count()
        == 1
    )
    db_session.commit()

    with pytest.raises(ServiceExtensionError) as exc:
        svc.reverse_service_extension(
            db_session,
            svc.ReverseServiceExtensionCommand(
                context=_command_context(
                    svc.REVERSE_SCOPE,
                    idempotency_key=svc.transition_idempotency_key(
                        extension.id,
                        "reverse",
                    ),
                    reason="A different reason",
                ),
                extension_id=extension.id,
                preview_fingerprint="0" * 64,
            ),
        )
    assert exc.value.code.endswith("idempotency_conflict")


def test_reverse_rejects_stale_preview_without_partial_write(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Mistaken grant",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )
    _apply(db_session, extension.id)
    preview = svc.preview_service_extension_reversal(
        db_session,
        extension_id=extension.id,
        reason="Wrong outage selected",
    )
    subscription.next_billing_at = subscription.next_billing_at + timedelta(days=1)
    db_session.commit()

    with pytest.raises(ServiceExtensionError) as exc:
        svc.reverse_service_extension(
            db_session,
            svc.ReverseServiceExtensionCommand(
                context=_command_context(
                    svc.REVERSE_SCOPE,
                    idempotency_key=svc.transition_idempotency_key(
                        extension.id,
                        "reverse",
                    ),
                    reason="Wrong outage selected",
                ),
                extension_id=extension.id,
                preview_fingerprint=preview.fingerprint,
            ),
        )

    assert exc.value.code.endswith("stale_reversal_preview")
    stored = db_session.get(ServiceExtension, extension.id)
    assert stored is not None and stored.status == ServiceExtensionStatus.applied
    assert db_session.query(ServiceExtensionReversal).count() == 0


def test_reverse_rolls_back_anchor_and_evidence_when_audit_staging_fails(
    db_session,
    subscriber,
    catalog_offer,
    monkeypatch,
):
    subscription = _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Mistaken grant",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )
    _apply(db_session, extension.id)
    applied_anchor = subscription.next_billing_at
    preview = svc.preview_service_extension_reversal(
        db_session,
        extension_id=extension.id,
        reason="Wrong outage selected",
    )
    db_session.commit()

    def fail_evidence(*_args, **_kwargs):
        raise RuntimeError("forced reversal evidence failure")

    monkeypatch.setattr(svc, "_stage_lifecycle_evidence", fail_evidence)
    with pytest.raises(RuntimeError, match="forced reversal evidence failure"):
        svc.reverse_service_extension(
            db_session,
            svc.ReverseServiceExtensionCommand(
                context=_command_context(
                    svc.REVERSE_SCOPE,
                    idempotency_key=svc.transition_idempotency_key(
                        extension.id,
                        "reverse",
                    ),
                    reason="Wrong outage selected",
                ),
                extension_id=extension.id,
                preview_fingerprint=preview.fingerprint,
            ),
        )

    stored = db_session.get(ServiceExtension, extension.id)
    db_session.refresh(subscription)
    assert stored is not None and stored.status == ServiceExtensionStatus.applied
    assert _naive(subscription.next_billing_at) == _naive(applied_anchor)
    assert db_session.query(ServiceExtensionReversal).count() == 0
    assert db_session.query(ServiceExtensionReversalEntry).count() == 0


def test_create_and_cancel_record_actor_audit(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Audited compensation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
        created_by="admin-1",
    )
    _cancel(db_session, extension.id, actor_id="admin-1")

    actions = set(
        db_session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_type == "service_extension",
                AuditEvent.entity_id == str(extension.id),
            )
        ).all()
    )
    assert actions == {
        "billing.service_extension_created",
        "billing.service_extension_canceled",
    }


def test_subscribers_scope_requires_ids(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    with pytest.raises(ServiceExtensionError) as exc:
        _create(
            db_session,
            reason="outage",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=1,
            scope_type=ServiceExtensionScope.subscribers,
            subscriber_ids=[],
        )
    assert exc.value.code.endswith("empty_subscriber_scope")


def test_subscriber_scope_inputs_prefers_selected_uuid_and_keeps_legacy_textarea():
    selected = str(uuid4())

    assert _subscriber_scope_inputs([selected], "ACC-IGNORED") == ([selected], True)
    assert _subscriber_scope_inputs(["ACC-EXT-1\nACC-EXT-2"], None) == (
        ["ACC-EXT-1", "ACC-EXT-2"],
        False,
    )
    assert _subscriber_scope_inputs(None, "ACC-EXT-3\n\nACC-EXT-4") == (
        ["ACC-EXT-3", "ACC-EXT-4"],
        False,
    )


def test_service_extension_failure_diagnostics_reports_counts_without_identifiers():
    selected = str(uuid4())
    request = SimpleNamespace(
        state=SimpleNamespace(request_id="req-123"),
        url=SimpleNamespace(path="/admin/billing/service-extensions"),
        method="POST",
    )

    diagnostics = _service_extension_failure_diagnostics(
        request,
        detail="At least one subscriber is required",
        reason="outage",
        window_start="2026-06-10T08:00",
        window_end="2026-06-10T20:00",
        days=1,
        scope_type="subscribers",
        scope_id="",
        subscriber_ids=["", selected],
        subscriber_identifiers="ACC-EXT-1\nACC-EXT-2",
        resolved_ids=[selected],
        ids_resolved=True,
    )

    assert diagnostics["event"] == "service_extension_create_failed"
    assert diagnostics["request_id"] == "req-123"
    assert diagnostics["subscriber_ids_field_count"] == 2
    assert diagnostics["subscriber_ids_nonblank_count"] == 1
    assert diagnostics["subscriber_ids_uuid_count"] == 1
    assert diagnostics["subscriber_identifiers_line_count"] == 2
    assert diagnostics["resolved_subscriber_count"] == 1
    assert "ACC-EXT-1" not in diagnostics.values()
    assert selected not in diagnostics.values()


def test_subscribers_scope_resolves_customer_identifiers(
    db_session, subscriber, catalog_offer
):
    subscriber.account_number = "ACC-EXT-1"
    subscriber.splynx_customer_id = 11192
    subscriber.phone = "08012345678"
    _sub(db_session, subscriber, catalog_offer)

    by_email = _another_subscriber(db_session)
    by_email.email = "billing-ext@example.com"
    _sub(db_session, by_email, catalog_offer)
    db_session.commit()

    ext = _create(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[
            "ACC-EXT-1",
            "11192",
            "08012345678",
            "billing-ext@example.com",
            str(by_email.id),
        ],
    )

    assert set(ext.scope_subscriber_ids or []) == {
        str(subscriber.id),
        str(by_email.id),
    }
    preview = svc.preview_extension(db_session, ext)
    assert preview.extendable_count == 2


def test_subscriber_uuid_scope_skips_identity_resolution(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _sub(
        db_session,
        subscriber,
        catalog_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    def fail_identity_resolution(*_args, **_kwargs):
        raise AssertionError("stored subscriber UUID scopes must not be re-resolved")

    monkeypatch.setattr(svc, "resolve_customer_identity", fail_identity_resolution)

    ext = _create(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[str(subscriber.id), str(subscriber.id)],
        subscriber_ids_resolved=True,
    )

    assert ext.scope_subscriber_ids == [str(subscriber.id)]

    preview = svc.preview_extension(db_session, ext)
    assert preview.total_count == 1
    assert preview.extendable_count == 1
    assert [item.id for item in preview.selected_subscribers] == [subscriber.id]

    applied = _apply(db_session, ext.id)
    assert applied.affected_count == 1
    db_session.refresh(subscription)
    assert _naive(subscription.next_billing_at) == datetime(2026, 7, 2)


def test_resolved_subscriber_scope_rejects_missing_uuid(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)
    missing = uuid4()

    with pytest.raises(ServiceExtensionError) as exc:
        _create(
            db_session,
            reason="outage",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=1,
            scope_type=ServiceExtensionScope.subscribers,
            subscriber_ids=[str(missing)],
            subscriber_ids_resolved=True,
        )

    assert exc.value.code.endswith("customer_not_found")
    assert exc.value.details["identifier"] == str(missing)


def test_subscribers_scope_reports_unknown_customer(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)

    with pytest.raises(ServiceExtensionError) as exc:
        _create(
            db_session,
            reason="outage",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=1,
            scope_type=ServiceExtensionScope.subscribers,
            subscriber_ids=["not-a-customer"],
        )

    assert exc.value.code.endswith("customer_not_found")
    assert exc.value.details["identifier"] == "not-a-customer"


def test_shared_contact_email_is_ambiguous(db_session):
    # Post-decoupling, subscribers.email is non-unique: many customers can share
    # a contact email. Resolving by such an email must refuse as ambiguous
    # (steering to the internal UUID), not silently pick one.
    a = Subscriber(first_name="A", last_name="One", email="shared@ext.example")
    b = Subscriber(first_name="B", last_name="Two", email="shared@ext.example")
    db_session.add_all([a, b])
    db_session.commit()

    with pytest.raises(ServiceExtensionError) as exc:
        svc._find_subscriber_by_identifier(db_session, "shared@ext.example")
    assert exc.value.code.endswith("ambiguous_customer_identifier")
    assert "ambiguous" in exc.value.message.lower()


def test_long_digit_identifier_not_treated_as_splynx_id(db_session):
    # An 11-digit string exceeds int4; it must NOT hit the imported customer id
    # branch (which would overflow the int4 column on Postgres → 500). With no
    # phone match it is simply "not found".
    with pytest.raises(ServiceExtensionError) as exc:
        svc._find_subscriber_by_identifier(db_session, "99999999999")
    assert exc.value.code.endswith("customer_not_found")
    assert "not found" in exc.value.message.lower()


def _suspend(db_session, subscription, reason):
    from app.services.account_lifecycle import suspend_subscription

    suspend_subscription(
        db_session, str(subscription.id), reason=reason, source="test", emit=False
    )
    db_session.commit()
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_apply_resumes_billing_suspended_subscription(
    db_session, subscriber, catalog_offer
):
    from app.models.enforcement_lock import EnforcementLock, EnforcementReason

    sub = _sub(db_session, subscriber, catalog_offer)
    _suspend(db_session, sub, EnforcementReason.overdue)

    ext = _create(
        db_session,
        reason="outage compensation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=5,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[str(subscriber.id)],
        created_by="admin-1",
    )
    applied = _apply(db_session, ext.id, actor_id="approver-1")

    assert applied.affected_count == 1
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert _naive(sub.next_billing_at) == datetime(2026, 7, 6)
    lock = (
        db_session.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id == sub.id)
        .one()
    )
    assert lock.is_active is False
    assert f"service_extension:{ext.id}" == lock.resolved_by


def test_apply_does_not_lift_admin_or_fraud_suspension(
    db_session, subscriber, catalog_offer
):
    from app.models.enforcement_lock import EnforcementReason

    sub = _sub(db_session, subscriber, catalog_offer)
    _suspend(db_session, sub, EnforcementReason.fraud)

    ext = _create(
        db_session,
        reason="outage compensation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=5,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[str(subscriber.id)],
        created_by="admin-1",
    )
    _apply(db_session, ext.id, actor_id="approver-1")

    db_session.refresh(sub)
    # Validity still extended, but the fraud hold stays.
    assert sub.status == SubscriptionStatus.suspended
    assert _naive(sub.next_billing_at) == datetime(2026, 7, 6)


def test_extension_shield_covers_window_then_expires(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)
    ext = _create(
        db_session,
        reason="outage compensation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=5,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[str(subscriber.id)],
        created_by="admin-1",
    )
    _apply(db_session, ext.id, actor_id="approver-1")

    reason = svc.extension_shield_reason(db_session, subscriber.id)
    assert reason is not None and str(ext.id) in reason

    # Move the canonical grant end into the past: no shield.
    entry = (
        db_session.query(ServiceExtensionEntry)
        .filter(ServiceExtensionEntry.extension_id == ext.id)
        .one()
    )
    entry.grant_ends_at = _APPLIED_AT - timedelta(days=1)
    db_session.commit()
    assert svc.extension_shield_reason(db_session, subscriber.id) is None


def test_dunning_shield_includes_extension(db_session, subscriber, catalog_offer):
    from app.services.collections._core import _dunning_shield_reason

    _sub(db_session, subscriber, catalog_offer)
    assert _dunning_shield_reason(db_session, subscriber.id) is None

    ext = _create(
        db_session,
        reason="outage compensation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=5,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_ids=[str(subscriber.id)],
        created_by="admin-1",
    )
    _apply(db_session, ext.id, actor_id="approver-1")

    reason = _dunning_shield_reason(db_session, subscriber.id)
    assert reason is not None and "service extension" in reason


def test_create_stages_one_atomic_audit_and_domain_event(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)
    subscriber_id = str(subscriber.id)
    db_session.commit()
    idempotency_key = str(uuid4())
    context = _command_context(
        svc.CREATE_SCOPE,
        idempotency_key=idempotency_key,
    )
    command = svc.CreateServiceExtensionCommand(
        context=context,
        reason="Atomic creation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_identifiers=(subscriber_id,),
        subscriber_ids_resolved=True,
    )

    outcome = svc.create_service_extension(db_session, command)
    replay = svc.create_service_extension(db_session, command)

    assert outcome.replayed is False
    assert replay.replayed is True
    assert replay.extension_id == outcome.extension_id
    assert replay.command_id == outcome.command_id
    audits = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "service_extension",
            AuditEvent.entity_id == str(outcome.extension_id),
            AuditEvent.action == "billing.service_extension_created",
        )
        .all()
    )
    events = (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "billing.service_extension_created",
            EventStore.payload["extension_id"].as_string() == str(outcome.extension_id),
        )
        .all()
    )
    assert len(audits) == 1
    assert audits[0].actor_label == "Staff member"
    assert audits[0].metadata_["command_id"] == str(context.command_id)
    assert "idempotency_key_sha256" in audits[0].metadata_
    assert len(events) == 1
    assert (
        db_session.query(ServiceExtension)
        .filter(ServiceExtension.id == outcome.extension_id)
        .count()
        == 1
    )


def test_create_rejects_changed_inputs_for_same_idempotency_key(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)
    subscriber_id = str(subscriber.id)
    db_session.commit()
    context = _command_context(
        svc.CREATE_SCOPE,
        idempotency_key=str(uuid4()),
    )
    first = svc.CreateServiceExtensionCommand(
        context=context,
        reason="Same command",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_identifiers=(subscriber_id,),
        subscriber_ids_resolved=True,
    )
    changed = svc.CreateServiceExtensionCommand(
        context=context,
        reason="Same command",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_identifiers=(subscriber_id,),
        subscriber_ids_resolved=True,
    )

    svc.create_service_extension(db_session, first)
    with pytest.raises(ServiceExtensionError) as exc:
        svc.create_service_extension(db_session, changed)

    assert exc.value.code.endswith("idempotency_conflict")
    assert db_session.query(ServiceExtension).count() == 1


def test_forced_create_failure_rolls_back_aggregate_audit_and_event(
    db_session, subscriber, catalog_offer, monkeypatch
):
    _sub(db_session, subscriber, catalog_offer)
    subscriber_id = str(subscriber.id)
    db_session.commit()
    command = svc.CreateServiceExtensionCommand(
        context=_command_context(svc.CREATE_SCOPE),
        reason="Rollback creation",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.subscribers,
        subscriber_identifiers=(subscriber_id,),
        subscriber_ids_resolved=True,
    )

    def fail_evidence(*_args, **_kwargs):
        raise RuntimeError("forced create evidence failure")

    monkeypatch.setattr(svc, "_stage_lifecycle_evidence", fail_evidence)
    with pytest.raises(RuntimeError, match="forced create evidence failure"):
        svc.create_service_extension(db_session, command)

    assert (
        db_session.query(ServiceExtension)
        .filter(ServiceExtension.reason == "Rollback creation")
        .count()
        == 0
    )
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "billing.service_extension_created")
        .count()
        == 0
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "billing.service_extension_created")
        .count()
        == 0
    )


def test_apply_stages_entries_audit_and_events_once(
    db_session, subscriber, catalog_offer
):
    subscription = _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Atomic apply",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )

    first = _apply(db_session, extension.id)
    second = _apply(db_session, extension.id)

    assert first.replayed is False
    assert second.replayed is True
    assert (
        db_session.query(ServiceExtensionEntry)
        .filter(ServiceExtensionEntry.extension_id == extension.id)
        .count()
        == 1
    )
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "service_extension",
            AuditEvent.entity_id == str(extension.id),
            AuditEvent.action == "billing.service_extension_applied",
        )
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "billing.service_extension_applied",
            EventStore.payload["extension_id"].as_string() == str(extension.id),
        )
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "billing.service_extended",
            EventStore.subscription_id == subscription.id,
        )
        .count()
        == 1
    )


def test_forced_apply_failure_rolls_back_state_entries_audit_and_events(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _sub(db_session, subscriber, catalog_offer)
    previous_anchor = subscription.next_billing_at
    extension = _create(
        db_session,
        reason="Rollback apply",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )

    def fail_evidence(*_args, **_kwargs):
        raise RuntimeError("forced lifecycle evidence failure")

    monkeypatch.setattr(svc, "_stage_lifecycle_evidence", fail_evidence)
    with pytest.raises(RuntimeError, match="forced lifecycle evidence failure"):
        _apply(db_session, extension.id)

    stored = db_session.get(ServiceExtension, extension.id)
    db_session.refresh(subscription)
    assert stored is not None
    assert stored.status == ServiceExtensionStatus.pending
    assert _naive(subscription.next_billing_at) == _naive(previous_anchor)
    assert (
        db_session.query(ServiceExtensionEntry)
        .filter(ServiceExtensionEntry.extension_id == extension.id)
        .count()
        == 0
    )
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == str(extension.id),
            AuditEvent.action == "billing.service_extension_applied",
        )
        .count()
        == 0
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.payload["extension_id"].as_string() == str(extension.id),
            EventStore.event_type.in_(
                (
                    "billing.service_extended",
                    "billing.service_extension_applied",
                )
            ),
        )
        .count()
        == 0
    )


def test_cancel_is_atomic_replayable_and_preserves_apply_actor(
    db_session, subscriber, catalog_offer
):
    _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Cancel evidence",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
    )
    stored = db_session.get(ServiceExtension, extension.id)
    assert stored is not None
    stored.applied_by = "legacy-apply-actor"
    db_session.commit()

    first = _cancel(db_session, extension.id, actor_id="cancel-actor")
    second = _cancel(db_session, extension.id, actor_id="cancel-actor")

    assert first.replayed is False
    assert second.replayed is True
    stored = db_session.get(ServiceExtension, extension.id)
    assert stored is not None
    assert stored.applied_by == "legacy-apply-actor"
    assert stored.canceled_by == "cancel-actor"
    assert stored.canceled_at is not None
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == str(extension.id),
            AuditEvent.action == "billing.service_extension_canceled",
        )
        .count()
        == 1
    )


def test_anchor_projection_repair_is_bounded_and_idempotent(
    db_session, subscriber, catalog_offer
):
    subscription = _sub(db_session, subscriber, catalog_offer)
    extension = _create(
        db_session,
        reason="Repairable extension",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )
    _apply(db_session, extension.id)
    db_session.refresh(subscription)
    expected_anchor = subscription.next_billing_at
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.commit()

    first = svc.repair_service_extension_anchor_projection(
        db_session,
        svc.RepairServiceExtensionAnchorProjectionCommand(
            context=_command_context(svc.APPLY_SCOPE),
            extension_id=extension.id,
        ),
    )
    second = svc.repair_service_extension_anchor_projection(
        db_session,
        svc.RepairServiceExtensionAnchorProjectionCommand(
            context=_command_context(svc.APPLY_SCOPE),
            extension_id=extension.id,
        ),
    )

    db_session.refresh(subscription)
    assert first.inspected_count == 1
    assert first.repaired_count == 1
    assert second.repaired_count == 0
    assert _naive(subscription.next_billing_at) == _naive(expected_anchor)
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == str(extension.id),
            AuditEvent.action == "billing.service_extension_anchor_repaired",
        )
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "billing.service_extension_anchor_repaired",
            EventStore.payload["extension_id"].as_string() == str(extension.id),
        )
        .count()
        == 1
    )
