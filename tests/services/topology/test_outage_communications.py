"""network.outage_communications: customer outage messages (§3).

Pins the four rules the design turns on: exposure is never a message, the
recovery cohort comes from delivery lineage rather than the current audience,
one customer gets one message however many services they hold, and clearing →
reopened opens a second conversation without re-sending the first.

Also pins the cutover invariant — arming this owner stands the legacy
outage_notifications paths down, so two customer outage senders can never be
live at once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.network_monitoring import (
    CustomerOutageInterval,
    NetworkDevice,
    OutageCustomerNotice,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.settings_cache import SettingsCache
from app.services.topology import outage_communications
from app.services.topology.outage import (
    confirm_incident,
    declare_outage,
    open_classifier_incident,
    reopen_incident,
    resolve_outage,
    start_clearing,
)
from app.services.topology.outage_communications import (
    NoticeStage,
    NoticeStatus,
    OutageCommunicationsDriftError,
    plan_incident_notices,
    send_incident_notices,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


# --- fixtures ---------------------------------------------------------------


def _gate(db, key: str, value: str, value_type=SettingValueType.boolean) -> None:
    row = (
        db.query(DomainSetting)
        .filter_by(domain=SettingDomain.network_monitoring, key=key)
        .one_or_none()
    )
    if row is None:
        row = DomainSetting(
            domain=SettingDomain.network_monitoring,
            key=key,
            value_type=value_type,
            value_text=value,
            is_active=True,
        )
        db.add(row)
    else:
        row.value_type = value_type
        row.value_text = value
        row.value_json = None
        row.is_active = True
    db.flush()
    SettingsCache.invalidate(SettingDomain.network_monitoring.value, key)


def _armed(db, *, dry_run: bool = False, min_affected: int = 1) -> None:
    """Arm the owner for a live send with the safety gates opened up."""

    _gate(db, "outage_customer_comms_enabled", "true")
    _gate(db, "outage_customer_comms_dry_run", "true" if dry_run else "false")
    _gate(
        db,
        "outage_customer_comms_min_affected",
        str(min_affected),
        SettingValueType.integer,
    )
    _gate(
        db,
        "outage_customer_comms_settle_minutes",
        "1",
        SettingValueType.integer,
    )


def _node_with_customers(db, offer_id, count, *, ip="10.9.0.1", services_each=1):
    """One access node with ``count`` customers behind it."""

    nas = NasDevice(name=f"NAS-{ip}", management_ip=ip)
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name=f"comms-node-{ip}",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    customers = []
    for index in range(count):
        subscriber = Subscriber(
            first_name="Ada",
            last_name=str(index),
            email=f"comms-{index}-{nas.id}@example.test",
        )
        db.add(subscriber)
        db.flush()
        subscriptions = []
        for _ in range(services_each):
            subscription = Subscription(
                subscriber_id=subscriber.id,
                offer_id=offer_id,
                status=SubscriptionStatus.active,
                provisioning_nas_device_id=nas.id,
            )
            db.add(subscription)
            subscriptions.append(subscription)
        customers.append((subscriber, subscriptions))
    db.flush()
    return nas, node, customers


def _live_session(db, nas, subscription):
    row = RadiusActiveSession(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        nas_device_id=nas.id,
        username=f"user-{subscription.id}",
        acct_session_id=f"sess-{subscription.id}",
        session_start=NOW,
    )
    db.add(row)
    db.flush()
    return row


def _notices(db, incident, *, stage: NoticeStage | None = None):
    query = db.query(OutageCustomerNotice).filter(
        OutageCustomerNotice.incident_id == incident.id
    )
    if stage is not None:
        query = query.filter(OutageCustomerNotice.stage == stage.value)
    return query.all()


# --- exposure is not a message ---------------------------------------------


def test_suspected_incident_tells_nobody(db_session, catalog_offer):
    """A suspected incident is exposure only: a false 'your area is down' is
    worse than silence."""

    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 4)
    incident = open_classifier_incident(
        db_session, root_node=node, now=NOW - timedelta(hours=1)
    )

    plan = plan_incident_notices(db_session, incident, now=NOW)

    assert plan.candidates == ()
    assert plan.opened == 0


def test_customer_with_live_session_is_not_told(db_session, catalog_offer):
    """Continued service proves the customer is not down, even though their
    path traverses the failed boundary."""

    _armed(db_session)
    nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 3)
    online_subscriber, online_subs = customers[0]
    _live_session(db_session, nas, online_subs[0])
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    plan = plan_incident_notices(db_session, incident, now=NOW)

    told = {candidate.subscriber_id for candidate in plan.candidates}
    assert online_subscriber.id not in told
    assert len(told) == 2


def test_settling_window_holds_the_opening_message(db_session, catalog_offer):
    """A blip that self-clears inside the settling window never reaches a
    customer."""

    _armed(db_session)
    _gate(
        db_session,
        "outage_customer_comms_settle_minutes",
        "30",
        SettingValueType.integer,
    )
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 4)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(minutes=5)
    db_session.flush()

    plan = plan_incident_notices(db_session, incident, now=NOW)

    assert plan.candidates == ()
    assert plan.gated_reason == "not_settled"


def test_below_minimum_affected_is_not_an_area_message(db_session, catalog_offer):
    _armed(db_session, min_affected=5)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    plan = plan_incident_notices(db_session, incident, now=NOW)

    assert plan.candidates == ()
    assert plan.gated_reason == "below_min_affected"


# --- one customer, one message ---------------------------------------------


def test_multi_service_customer_is_told_once(db_session, catalog_offer):
    """A customer with two services behind one splitter has one outage, so
    they get one message naming both services."""

    _armed(db_session)
    _nas, node, customers = _node_with_customers(
        db_session, catalog_offer.id, 2, services_each=2
    )
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    plan = plan_incident_notices(db_session, incident, now=NOW)

    assert len(plan.candidates) == 2
    for candidate in plan.candidates:
        assert len(candidate.subscription_ids) == 2
        assert "your 2 services" in candidate.body


# --- the recovery cohort comes from lineage --------------------------------


def test_restoration_goes_to_who_we_told_not_the_current_audience(
    db_session, catalog_offer
):
    """The design's hard rule: a mid-incident joiner was never promised
    anything, and a customer who left still deserves the all-clear."""

    _armed(db_session)
    nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    send_incident_notices(db_session, incident, actor="test", now=NOW)
    opened = _notices(db_session, incident, stage=NoticeStage.opened)
    assert len(opened) == 3
    told = {row.subscriber_id for row in opened}

    # A fourth customer joins the audience only after the opening message.
    late_subscriber = Subscriber(
        first_name="Late", last_name="Joiner", email="late@example.test"
    )
    db_session.add(late_subscriber)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=late_subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas.id,
        )
    )
    db_session.flush()

    resolve_outage(db_session, incident.id)
    db_session.flush()
    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(hours=2)
    )

    restored = _notices(db_session, incident, stage=NoticeStage.restored)
    assert {row.subscriber_id for row in restored} == told
    assert late_subscriber.id not in {row.subscriber_id for row in restored}


def test_suppressed_customer_is_not_in_the_recovery_cohort(db_session, catalog_offer):
    """We never told them, so we do not tell them it is fixed."""

    _armed(db_session)
    _nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 3)
    suppressed_subscriber, _subs = customers[0]
    # The notification policy refuses a canceled customer; their opening
    # message is recorded as suppressed and carries no delivery lineage.
    suppressed_subscriber.status = SubscriberStatus.canceled
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    send_incident_notices(db_session, incident, actor="test", now=NOW)
    opened = [
        row
        for row in _notices(db_session, incident, stage=NoticeStage.opened)
        if row.subscriber_id == suppressed_subscriber.id
    ]
    assert opened and opened[0].status == NoticeStatus.suppressed.value

    resolve_outage(db_session, incident.id)
    db_session.flush()
    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(hours=2)
    )

    restored = _notices(db_session, incident, stage=NoticeStage.restored)
    assert suppressed_subscriber.id not in {row.subscriber_id for row in restored}
    assert len(restored) == 2


def test_discarded_incident_still_closes_the_conversation(db_session, catalog_offer):
    """A false positive we announced is still an announcement to retract."""

    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = open_classifier_incident(
        db_session, root_node=node, now=NOW - timedelta(hours=1)
    )
    confirm_incident(db_session, incident, now=NOW - timedelta(hours=1))
    incident.started_at = NOW - timedelta(hours=1)
    incident.confirmed_at = NOW - timedelta(hours=1)
    db_session.flush()

    send_incident_notices(db_session, incident, actor="test", now=NOW)
    assert len(_notices(db_session, incident, stage=NoticeStage.opened)) == 3

    incident.status = "discarded"
    db_session.flush()
    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(hours=1)
    )

    assert len(_notices(db_session, incident, stage=NoticeStage.restored)) == 3


def test_partial_restoration_closes_only_recovered_customers(db_session, catalog_offer):
    _armed(db_session)
    nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = open_classifier_incident(
        db_session, root_node=node, now=NOW - timedelta(hours=1)
    )
    confirm_incident(db_session, incident, now=NOW - timedelta(hours=1))
    incident.started_at = NOW - timedelta(hours=1)
    incident.confirmed_at = NOW - timedelta(hours=1)
    db_session.flush()
    send_incident_notices(db_session, incident, actor="test", now=NOW)

    # One customer comes back while the incident is still clearing.
    recovered_subscriber, recovered_subs = customers[0]
    _live_session(db_session, nas, recovered_subs[0])
    start_clearing(db_session, incident, now=NOW)
    db_session.flush()

    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(minutes=30)
    )

    restored = _notices(db_session, incident, stage=NoticeStage.restored)
    assert {row.subscriber_id for row in restored} == {recovered_subscriber.id}


# --- episodes ---------------------------------------------------------------


def test_reopened_incident_opens_a_second_conversation(db_session, catalog_offer):
    """clearing → reopened is one ledger interval but two conversations: the
    customer was already told it was fixed."""

    _armed(db_session)
    nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 2)
    incident = open_classifier_incident(
        db_session, root_node=node, now=NOW - timedelta(hours=1)
    )
    confirm_incident(db_session, incident, now=NOW - timedelta(hours=1))
    incident.started_at = NOW - timedelta(hours=1)
    incident.confirmed_at = NOW - timedelta(hours=1)
    db_session.flush()
    send_incident_notices(db_session, incident, actor="test", now=NOW)

    for _subscriber, subs in customers:
        _live_session(db_session, nas, subs[0])
    start_clearing(db_session, incident, now=NOW)
    db_session.flush()
    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(minutes=10)
    )
    assert len(_notices(db_session, incident, stage=NoticeStage.restored)) == 2

    # It re-darkens.
    db_session.query(RadiusActiveSession).delete()
    reopen_incident(db_session, incident)
    db_session.flush()
    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(minutes=30)
    )

    opened = _notices(db_session, incident, stage=NoticeStage.opened)
    assert len(opened) == 4
    assert {row.sequence for row in opened} == {1, 2}


def test_prolonged_outage_sends_one_update_per_interval(db_session, catalog_offer):
    _armed(db_session)
    _gate(
        db_session,
        "outage_customer_comms_update_interval_hours",
        "6",
        SettingValueType.integer,
    )
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()
    send_incident_notices(db_session, incident, actor="test", now=NOW)

    # Too soon: nothing owed.
    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(hours=2)
    )
    assert _notices(db_session, incident, stage=NoticeStage.update) == []

    send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(hours=7)
    )
    updates = _notices(db_session, incident, stage=NoticeStage.update)
    assert len(updates) == 2
    assert {row.sequence for row in updates} == {1}


# --- idempotency ------------------------------------------------------------


def test_repeated_send_is_a_no_op(db_session, catalog_offer):
    """A replayed lifecycle event must not double-message anybody.

    The conversation history alone is enough: once a customer has been told,
    no stage is owed, so a replay produces no candidates at all.
    """

    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    first = send_incident_notices(db_session, incident, actor="test", now=NOW)
    second = send_incident_notices(db_session, incident, actor="test", now=NOW)

    assert first.queued == 3
    assert second.queued == 0
    assert second.reason == "nothing_owed"
    assert len(_notices(db_session, incident)) == 3


def test_dedupe_key_is_the_last_line_of_defence(db_session, catalog_offer):
    """History is the first guard; the unique key is the one that holds when
    two workers decide the same message concurrently."""

    from sqlalchemy.exc import IntegrityError

    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()
    send_incident_notices(db_session, incident, actor="test", now=NOW)

    existing = _notices(db_session, incident)[0]
    db_session.add(
        OutageCustomerNotice(
            incident_id=incident.id,
            subscriber_id=existing.subscriber_id,
            stage=existing.stage,
            sequence=existing.sequence,
            status=NoticeStatus.queued.value,
            dedupe_key=existing.dedupe_key,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_dry_run_records_the_plan_without_sending(db_session, catalog_offer):
    """Dry run must leave measurable evidence — ADR 0004's dry run only
    logged, which is why nobody could evaluate it."""

    _armed(db_session, dry_run=True)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    result = send_incident_notices(db_session, incident, actor="test", now=NOW)

    assert result.queued == 0
    assert result.planned == 3
    rows = _notices(db_session, incident)
    assert {row.status for row in rows} == {NoticeStatus.planned_dry_run.value}
    assert all(row.communication_intent_id is None for row in rows)


def test_dry_run_rows_never_mute_a_later_real_send(db_session, catalog_offer):
    """A plan is not a promise: arming for real must still reach everybody."""

    _armed(db_session, dry_run=True)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()
    send_incident_notices(db_session, incident, actor="test", now=NOW)

    _gate(db_session, "outage_customer_comms_dry_run", "false")
    result = send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(minutes=1)
    )

    assert result.queued == 3


# --- measured downtime is quoted, never recomputed --------------------------


def test_restoration_quotes_the_ledger_downtime(db_session, catalog_offer):
    _armed(db_session)
    _nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 1)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=3)
    db_session.flush()
    send_incident_notices(db_session, incident, actor="test", now=NOW)

    subscriber, subs = customers[0]
    db_session.add(
        CustomerOutageInterval(
            incident_id=incident.id,
            subscription_id=subs[0].id,
            state="confirmed_unavailable",
            quality="exact",
            started_at=NOW - timedelta(hours=3),
            ended_at=NOW - timedelta(hours=1),
            scope_revision_sequence=1,
            idempotency_key=f"{incident.id}:{subs[0].id}:test",
        )
    )
    resolve_outage(db_session, incident.id)
    db_session.flush()

    plan = plan_incident_notices(db_session, incident, now=NOW)
    restored = [c for c in plan.candidates if c.stage is NoticeStage.restored]
    assert len(restored) == 1
    assert "2 hours" in restored[0].body


# --- preview → confirm ------------------------------------------------------


def test_confirm_refuses_a_stale_preview(db_session, catalog_offer):
    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    with pytest.raises(OutageCommunicationsDriftError):
        send_incident_notices(
            db_session,
            incident,
            actor="test",
            now=NOW,
            expected_impact_token="stale" * 16,
        )
    assert _notices(db_session, incident) == []


def test_confirm_accepts_the_matching_preview(db_session, catalog_offer):
    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    plan = plan_incident_notices(db_session, incident, now=NOW)
    result = send_incident_notices(
        db_session,
        incident,
        actor="test",
        now=NOW,
        expected_impact_token=plan.impact_token,
    )

    assert result.queued == 3


# --- cutover: one canonical sender -----------------------------------------


def test_arming_supersedes_the_legacy_automatic_path(db_session, catalog_offer):
    from app.services.topology import outage_auto_notify

    _armed(db_session)
    _gate(db_session, "outage_auto_notify_enabled", "true")

    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        db_session, now=NOW
    )

    assert result["dispatched"] is False
    assert result["reason"] == "superseded_by_outage_communications"


def test_arming_supersedes_the_legacy_operator_path(db_session, catalog_offer):
    from app.services.topology import outage_notifications

    _armed(db_session)
    _nas, node, customers = _node_with_customers(db_session, catalog_offer.id, 2)
    incident = open_classifier_incident(
        db_session, root_node=node, now=NOW - timedelta(hours=1)
    )
    confirm_incident(db_session, incident, now=NOW - timedelta(hours=1))
    db_session.flush()

    result = outage_notifications.dispatch_outage_notifications(
        db_session,
        [subs[0].id for _subscriber, subs in customers],
        actor_id=uuid.uuid4(),
        incident_id=incident.id,
        now=NOW,
    )

    assert result["dispatched"] is False
    assert result["reason"] == "superseded_by_outage_communications"


def test_disarmed_owner_sends_nothing_through_the_consumer(db_session, catalog_offer):
    """The receipted consumer can be wired long before anyone arms it."""

    _gate(db_session, "outage_customer_comms_enabled", "false")
    assert outage_communications.is_armed(db_session) is False


# --- operator console wiring -----------------------------------------------


def _route(method: str, path: str):
    from app.web.admin.network_monitoring import router

    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return route
    return None


def _permission_of(route) -> str | None:
    for dep in route.dependant.dependencies:
        call = getattr(dep, "call", None)
        if call is None or getattr(call, "__name__", "") != "_require_permission":
            continue
        for cell in call.__closure__ or ():
            value = cell.cell_contents
            if isinstance(value, str) and ":" in value:
                return value
    return None


def test_console_routes_are_registered_and_gated():
    preview = _route("GET", "/network/outage-communications")
    send = _route("POST", "/network/outage-communications")
    assert preview is not None and send is not None
    assert _permission_of(preview) == "monitoring:read"
    # The only send path must be gated on write, not read.
    assert _permission_of(send) == "monitoring:write"


def test_console_template_compiles():
    from fastapi.templating import Jinja2Templates

    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/outage_communications.html"
    )


def test_operator_confirm_requires_a_preview_token(db_session, catalog_offer):
    _armed(db_session)
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    with pytest.raises(outage_communications.OutageCommunicationsError):
        outage_communications.confirm_incident_notices(
            db_session,
            incident,
            actor="operator",
            expected_impact_token=None,
            now=NOW,
        )


# --- the per-run cap defers, it does not cancel ------------------------------


def test_capped_customers_are_reached_on_the_next_pass(db_session, catalog_offer):
    """The cap bounds one pass. A customer it skipped must still be reachable
    — recording them under the canonical key would drop them silently."""

    _armed(db_session)
    _gate(
        db_session,
        "outage_customer_comms_max_recipients_per_run",
        "1",
        SettingValueType.integer,
    )
    _nas, node, _customers = _node_with_customers(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW - timedelta(hours=1)
    db_session.flush()

    first = send_incident_notices(db_session, incident, actor="test", now=NOW)
    assert first.queued == 1

    # A second capped pass must not collide on the deferral audit row either.
    second = send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(minutes=1)
    )
    assert second.queued == 1

    _gate(
        db_session,
        "outage_customer_comms_max_recipients_per_run",
        "500",
        SettingValueType.integer,
    )
    third = send_incident_notices(
        db_session, incident, actor="test", now=NOW + timedelta(minutes=2)
    )
    assert third.queued == 1

    queued = [
        row
        for row in _notices(db_session, incident, stage=NoticeStage.opened)
        if row.status == NoticeStatus.queued.value
    ]
    assert len({row.subscriber_id for row in queued}) == 3
