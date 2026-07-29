"""Deactivating a device is one atomic lifecycle slice, not a flag write.

The defect these tests pin down: ``is_active = False`` used to be written by
several callers directly, and each got only half a deactivation.

  1. ``collect_devices`` filtered the flag, and the projection reconciler
     deletes any row the derivation stops returning — so deactivation ERASED
     the device from the staff ledger.
  2. The topology warmer only visits pollable devices and nothing ever decayed
     ``live_status``, so a device warmed to ``up`` that left the pollable set
     stayed ``up`` forever.
  3. ``classify_node`` reads mgmt ``up`` + zero online customers as
     ``service_fault`` ("NOT an area outage"), so that frozen ``up`` silently
     vetoed outage detection and the customer surface reported the cabinet
     healthy indefinitely.

Release gate under test: an inactive or stale device can never project ``up``.

IMPORTANT: none of the NetworkDevice fixtures here set ``source``. The old
drift filter keyed on the retired ``zabbix_reconcile`` provenance and looked
healthy under test only because every existing fixture stamped it. Fixtures
without a source are what prove the replacement is authority-neutral.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.network_monitoring import (
    DeviceProjection,
    DeviceStatus,
    NetworkDevice,
)
from app.services import device_projection_reconcile as reconcile_mod
from app.services.device_operational_status import (
    NOT_WORKING,
    WORKING,
    derive_operational_status,
)
from app.services.device_projection_reconcile import (
    ReconcileDeviceProjectionsCommand,
    _gated_status,
    reconcile_device_projections,
)
from app.services.network_monitoring import (
    NetworkDevices,
    set_network_device_active,
)
from app.services.owner_commands import CommandContext
from app.services.topology.health_classifier import (
    NODE_OUTAGE,
    SERVICE_FAULT,
    UNKNOWN,
    classify_node,
)
from app.services.topology.live_status import (
    STALE_POLL_AFTER_SECONDS,
    trusted_live_status,
    warm_topology_status,
)
from app.services.web_network_core_devices_inventory import collect_devices

_ip_counter = iter(range(1, 10_000))


def _device(db, name, **kw):
    """A monitoring device with NO ``source`` — deliberately (see module docstring)."""
    kw.setdefault("is_active", True)
    kw.setdefault("ping_enabled", True)
    if "mgmt_ip" not in kw:
        n = next(_ip_counter)
        kw["mgmt_ip"] = f"10.88.{n // 250}.{n % 250 + 1}"
    device = NetworkDevice(name=name, **kw)
    db.add(device)
    db.flush()
    return device


def _fresh_up(db, name, **kw):
    """A device the poller currently observes as reachable."""
    now = datetime.now(UTC)
    return _device(
        db,
        name,
        live_status="up",
        live_status_at=now,
        last_ping_ok=True,
        last_ping_at=now,
        **kw,
    )


# ── 1. The owned transition ─────────────────────────────────────────────────


def test_deactivation_decays_live_status_instead_of_freezing_it(db_session):
    device = _fresh_up(db_session, "gwarimpa-4")

    set_network_device_active(db_session, device, False, reason="test")

    assert device.is_active is False
    # The whole point: the assertion is WITHDRAWN, not frozen at "up".
    assert device.live_status == "unknown"
    assert device.live_status_at is not None


def test_reactivation_does_not_resurrect_the_pre_deactivation_verdict(db_session):
    device = _fresh_up(db_session, "gwarimpa-5")
    set_network_device_active(db_session, device, False, reason="test")

    set_network_device_active(db_session, device, True, reason="test")

    assert device.is_active is True
    # Re-admission does not re-assert reachability; the next poll does.
    assert device.live_status == "unknown"


def test_transition_is_idempotent_and_repairs_a_previously_frozen_row(db_session):
    # A row deactivated before this transition existed: inactive but still
    # claiming "up". Re-applying the current state must repair it.
    device = _fresh_up(db_session, "gwarimpa-6", is_active=False)
    assert device.live_status == "up"

    set_network_device_active(db_session, device, False, reason="test")

    assert device.live_status == "unknown"


def test_soft_delete_routes_through_the_transition(db_session):
    device = _fresh_up(db_session, "manual-delete")

    NetworkDevices.delete(db_session, str(device.id))

    assert device.is_active is False
    assert device.live_status == "unknown"
    # Soft delete: the row is still there to be listed.
    assert db_session.get(NetworkDevice, device.id) is not None


def test_inventory_update_payload_routes_admission_through_the_transition(db_session):
    from app.schemas.network_monitoring import NetworkDeviceUpdate

    device = _fresh_up(db_session, "payload-deactivate")

    NetworkDevices.update(
        db_session, str(device.id), NetworkDeviceUpdate(is_active=False)
    )

    assert device.is_active is False
    assert device.live_status == "unknown"


def test_router_delete_cascade_decays_the_linked_monitoring_device(db_session):
    from app.models.router_management import Router
    from app.services.router_management.inventory import RouterInventory

    device = _fresh_up(db_session, "edge-router-node")
    router = Router(
        name="edge-router",
        hostname="edge-router.dotmac.internal",
        management_ip=device.mgmt_ip,
        rest_api_username="pytest",
        rest_api_password="pytest",
        network_device_id=device.id,
        is_active=True,
    )
    db_session.add(router)
    db_session.flush()

    RouterInventory.delete(db_session, router.id)

    db_session.refresh(device)
    assert device.is_active is False
    assert device.live_status == "unknown"


# ── 2. Inactive devices stay visible in inventory ───────────────────────────


def test_collect_devices_keeps_a_deactivated_core_device_with_an_inactive_marker(
    db_session,
):
    live = _fresh_up(db_session, "aa-live-core")
    dead = _fresh_up(db_session, "aa-dead-core")
    set_network_device_active(db_session, dead, False, reason="test")

    rows = {row["id"]: row for row in collect_devices(db_session)}

    # The regression: the deactivated device must NOT disappear.
    assert str(dead.id) in rows
    assert rows[str(dead.id)]["lifecycle_state"] == "inactive"
    assert rows[str(dead.id)]["status"] == NOT_WORKING
    assert rows[str(live.id)]["lifecycle_state"] == "active"


# ── 3. Release gate: inactive/stale can never project "up" ──────────────────


def _command() -> ReconcileDeviceProjectionsCommand:
    return ReconcileDeviceProjectionsCommand(
        context=CommandContext.system(
            actor="pytest:device_lifecycle",
            scope="network:test",
            reason="verify inactive devices stay projected",
        )
    )


def _projection_rows(db):
    return {
        (r.device_type, r.source_id): r
        for r in db.execute(select(DeviceProjection)).scalars()
    }


def test_reconcile_keeps_an_inactive_device_and_marks_it_instead_of_pruning(
    db_session,
):
    device = _fresh_up(db_session, "bb-dead-core")
    set_network_device_active(db_session, device, False, reason="test")
    # The owner command refuses a session that still carries a caller
    # transaction. Committing here ends the session-level transaction without
    # ending the fixture's outer connection transaction, so the setup rows stay
    # visible and the test still rolls back cleanly.
    db_session.commit()

    reconcile_device_projections(db_session, _command())

    rows = _projection_rows(db_session)
    row = rows[("core", str(device.id))]
    assert row.lifecycle_state == "inactive"
    assert row.operational_status == "not_working"


def test_gated_status_forces_inactive_devices_to_not_working():
    assert _gated_status("inactive", "working") == "not_working"
    assert _gated_status("inactive", "not_working") == "not_working"
    assert _gated_status("active", "working") == "working"


def test_reconcile_gates_an_upstream_derivation_that_claims_inactive_is_working(
    db_session, monkeypatch
):
    # Belt-and-braces: even if the derivation regressed and reported an
    # inactive device as working, the projection must not carry it.
    bad = {
        "id": str(uuid.uuid4()),
        "name": "liar",
        "type": "core",
        "serial_number": None,
        "ip_address": None,
        "vendor": None,
        "model": None,
        "status": "working",
        "operational_reason": None,
        "last_seen": None,
        "subscriber": None,
        "class_facts": None,
        "lifecycle_state": "inactive",
    }
    monkeypatch.setattr(reconcile_mod, "collect_devices", lambda db: [bad])

    reconcile_device_projections(db_session, _command())

    row = _projection_rows(db_session)[("core", bad["id"])]
    assert row.operational_status == "not_working"


def test_database_refuses_an_inactive_working_projection_row(db_session):
    """The release gate as a schema invariant, not just a code path.

    Scoped to a savepoint so the constraint violation unwinds only this insert
    — the fixture session is rollback-only, so an unscoped rollback would
    discard the whole test's state.
    """
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            DeviceProjection(
                device_type="core",
                source_id=str(uuid.uuid4()),
                operational_status="working",
                lifecycle_state="inactive",
                refreshed_at=datetime.now(UTC),
            )
        )
        db_session.flush()


# ── 4. The freshness gate on the read path ──────────────────────────────────


def test_trusted_live_status_passes_a_fresh_pollable_up(db_session):
    device = _fresh_up(db_session, "cc-fresh")
    assert trusted_live_status(device) == "up"


def test_trusted_live_status_decays_up_on_an_inactive_device(db_session):
    device = _fresh_up(db_session, "cc-inactive", is_active=False)
    assert trusted_live_status(device) == "unknown"


def test_trusted_live_status_does_not_decay_on_absent_data(db_session):
    """Missing columns mean "unknown", not "stale".

    The read gate decays only on positive evidence. A row that carries no poll
    columns and no dead-man reading is not proof of anything, and inferring
    staleness from an unhydrated row would fail closed on every caller that
    does not load them.
    """
    bare = NetworkDevice(name="cc-bare", live_status="up")

    assert trusted_live_status(bare) == "up"


def test_trusted_live_status_decays_up_when_the_observation_went_stale(db_session):
    stale = datetime.now(UTC) - timedelta(seconds=STALE_POLL_AFTER_SECONDS * 3)
    device = _device(
        db_session,
        "cc-stale",
        live_status="up",
        live_status_at=stale,
        last_ping_ok=True,
        last_ping_at=stale,
    )
    assert trusted_live_status(device) == "unknown"


def test_trusted_live_status_decays_up_when_the_warmer_died_under_a_live_poller(
    db_session,
):
    """The case the poll clock alone cannot catch.

    Poller alive, warmer dead: the poll columns keep advancing while
    ``live_status`` is frozen at whatever it last held, so the cache is behind
    its own evidence. Only the dead-man reading exposes that.
    """
    device = _fresh_up(db_session, "cc-warmer-dead")

    assert trusted_live_status(device, warm_stale=True) == "unknown"
    assert trusted_live_status(device, warm_stale=False) == "up"


def test_trusted_live_status_keeps_a_stale_down(db_session):
    """The gate is asymmetric on purpose.

    Stale ``up`` fails dangerously (it vetoes outage detection and tells the
    customer everything is fine). Stale ``down`` fails safely — it opens an
    incident an operator can see and close. Decaying both would let a dead
    warmer SUPPRESS real outages.
    """
    stale = datetime.now(UTC) - timedelta(seconds=STALE_POLL_AFTER_SECONDS * 3)
    device = _device(
        db_session,
        "cc-stale-down",
        live_status="down",
        live_status_at=stale,
        last_ping_ok=False,
        last_ping_at=stale,
    )
    assert trusted_live_status(device) == "down"


# ── 5. The frozen "up" no longer vetoes outage detection ────────────────────


def test_frozen_up_stops_asserting_reachability_instead_of_vetoing(db_session):
    """A frozen ``up`` must stop producing ``service_fault``.

    ``service_fault`` is an AFFIRMATIVE claim — "the node is reachable, it is
    just serving nobody, this is NOT an area outage" — and that claim is the
    veto: it is what suppressed the outage path and left the customer surface
    reporting the cabinet healthy.

    The correct replacement is ``unknown`` ("no mgmt evidence"), NOT
    ``node_outage``. An inactive device is not polled, so we have no
    observation of it at all; concluding "down" from an administrative flag
    would be inventory lifecycle manufacturing a reachability verdict, which
    is the same boundary violation this slice exists to remove, only inverted.
    ``unknown`` withdraws the claim without inventing its opposite.
    """
    node = _fresh_up(db_session, "dd-frozen-up", is_active=False)
    assert node.live_status == "up"  # frozen, exactly as production had it

    verdict = classify_node(node, online_count=0, had_prior_life=True)

    assert verdict != SERVICE_FAULT  # the veto is gone
    assert verdict == UNKNOWN


def test_an_actually_reachable_node_still_classifies_as_service_fault(db_session):
    # The guard must not swallow the genuine up/up/down row.
    node = _fresh_up(db_session, "dd-real-up")
    assert (
        classify_node(node, online_count=0, had_prior_life=True, warm_stale=False)
        == SERVICE_FAULT
    )


def test_a_dead_warmer_cannot_certify_a_node_reachable(db_session):
    # Fresh poll evidence, but nothing has recomputed live_status from it.
    node = _fresh_up(db_session, "dd-warmer-dead")

    verdict = classify_node(node, online_count=0, had_prior_life=True, warm_stale=True)

    assert verdict != SERVICE_FAULT
    assert verdict == UNKNOWN


def test_an_inactive_node_observed_down_still_opens_a_node_outage(db_session):
    """The gate must not make deactivated nodes un-outage-able.

    Only the positive assertion is gated. Negative evidence survives
    deactivation, so a node last seen ``down`` still reaches ``node_outage``
    and can still open an incident — the asymmetry is what keeps the fix from
    trading a silent false-healthy for a silent suppression.
    """
    node = _device(db_session, "dd-inactive-down", live_status="down", is_active=False)

    assert classify_node(node, online_count=0, had_prior_life=True) == NODE_OUTAGE


def test_frozen_up_no_longer_certifies_the_plant_and_blames_the_customer(
    db_session, monkeypatch
):
    """The customer-facing consequence that actually changed.

    ``_plant_is_up`` treats ``healthy``/``service_fault`` as proof the plant is
    up. With a frozen ``up`` that returned True, so the last-mile diagnoser
    concluded the fault was on the customer side and told them to check their
    equipment. Degrading to ``unknown`` makes it return False, which routes the
    customer to "we're still diagnosing" instead of a false accusation.
    """
    from app.services.topology import last_mile

    def _impact(session, node):
        return {"count": 12, "online_count": 0, "node_ids": [node.id]}

    monkeypatch.setattr(last_mile, "affected_customers", _impact)

    frozen = _fresh_up(db_session, "dd-plant-frozen", is_active=False)
    reachable = _fresh_up(db_session, "dd-plant-real")

    assert last_mile._plant_is_up(db_session, frozen, None, warm_stale=False) is False
    # A genuinely reachable node is still certified up — no over-correction.
    assert last_mile._plant_is_up(db_session, reachable, None, warm_stale=False) is True


# ── 6. The warmer decays what it no longer visits ───────────────────────────


def test_warm_topology_status_decays_nodes_that_left_the_pollable_set(db_session):
    """Repairs rows already frozen in production, not just new deactivations.

    Eligibility loss is repaired here, at the writer, rather than being
    re-derived on every read: the warmer knows exactly which rows it visited,
    so anything else holding a state is by definition unmaintained.
    """
    warmed = _fresh_up(db_session, "ee-still-polled")
    # Deactivated with a raw flag write, i.e. the pre-fix state: the derived
    # cache was never decayed.
    frozen = _fresh_up(db_session, "ee-frozen")
    frozen.is_active = False
    # Still active, but its checks were turned off — it silently left the poll
    # sweep and nothing would ever expire its "up" again.
    unchecked = _fresh_up(db_session, "ee-unchecked", ping_enabled=False)
    unchecked.snmp_enabled = False
    db_session.flush()
    assert frozen.live_status == "up"
    assert unchecked.live_status == "up"

    result = warm_topology_status(db_session)

    assert result["decayed"] >= 2
    assert frozen.live_status == "unknown"
    assert unchecked.live_status == "unknown"
    assert warmed.live_status == "up"


# ── 7. network.device_state honours admission and the poll clock ────────────


def test_derive_operational_status_marks_an_inactive_device_not_working(db_session):
    device = _fresh_up(db_session, "ff-inactive", is_active=False)

    operational = derive_operational_status(device, warm_stale=False)

    assert operational.status == NOT_WORKING
    assert operational.reason == "admin_inactive"
    # Deliberate removal from service is not an alarm.
    assert operational.alarming is False


def test_a_stably_up_device_is_working_despite_an_old_dwell_clock(db_session):
    """``live_status_at`` is a dwell clock, not a freshness signal.

    The warmer stamps it only when the state CHANGES, so a device up for a week
    carries a week-old ``live_status_at``. Reading that as observation age
    marked every stably-healthy device ``verification_expired``. Freshness must
    come from the poll clock.
    """
    now = datetime.now(UTC)
    device = _device(
        db_session,
        "ff-stable-up",
        status=DeviceStatus.online,
        live_status="up",
        live_status_at=now - timedelta(days=7),  # entered "up" a week ago
        last_ping_ok=True,
        last_ping_at=now,  # ...and was polled a moment ago
    )

    operational = derive_operational_status(device, warm_stale=False, now=now)

    assert operational.status == WORKING
    assert operational.reason == "observed_working"


def test_a_device_whose_poll_clock_stopped_is_not_working(db_session):
    now = datetime.now(UTC)
    device = _device(
        db_session,
        "ff-poll-stopped",
        live_status="up",
        live_status_at=now,  # dwell clock looks fresh...
        last_ping_ok=True,
        last_ping_at=now - timedelta(days=2),  # ...but nothing has polled it
    )

    operational = derive_operational_status(device, warm_stale=False, now=now)

    assert operational.status == NOT_WORKING
    assert operational.reason == "verification_expired"


# ── 8. The drift report is authority-neutral ────────────────────────────────


def test_unmatched_node_report_sees_devices_with_no_source_stamp(db_session):
    """The trap this whole area fell into.

    The old query required ``source == 'zabbix_reconcile'`` — provenance from
    an importer deleted on 2026-07-10 — so it was blind to every device created
    since. It only looked healthy under test because every existing fixture
    stamped that source. This device deliberately does not.
    """
    from app.services.topology.gaps import topology_gaps

    ghost = _device(db_session, "zz-no-source-ghost", matched_device_id=None)
    assert ghost.source is None

    gaps = topology_gaps(db_session)

    assert ghost.id in {node.id for node in gaps.unmatched_nodes}


def test_projection_drift_reports_a_device_missing_from_the_projection(db_session):
    from app.services.topology.gaps import (
        DRIFT_MISSING_PROJECTION,
        projection_drift_rows,
    )

    device = _device(db_session, "zz-unprojected")

    drift = {row["id"]: row["drift"] for row in projection_drift_rows(db_session)}

    assert drift.get(str(device.id)) == DRIFT_MISSING_PROJECTION


def test_projection_drift_reports_an_orphan_projection_row(db_session):
    from app.services.topology.gaps import (
        DRIFT_ORPHAN_PROJECTION,
        projection_drift_rows,
    )

    orphan_id = str(uuid.uuid4())
    db_session.add(
        DeviceProjection(
            device_type="core",
            source_id=orphan_id,
            name="zz-orphan",
            operational_status="not_working",
            lifecycle_state="active",
            refreshed_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    drift = {row["id"]: row["drift"] for row in projection_drift_rows(db_session)}

    assert drift.get(orphan_id) == DRIFT_ORPHAN_PROJECTION


def test_projection_drift_reports_a_reconciler_that_stopped_converging(db_session):
    from app.services.topology.gaps import (
        DRIFT_STALE_PROJECTION,
        PROJECTION_STALE_AFTER,
        projection_drift_rows,
    )

    device = _device(db_session, "zz-stale-projection")
    db_session.add(
        DeviceProjection(
            device_type="core",
            source_id=str(device.id),
            name=device.name,
            ip_address=device.mgmt_ip,
            operational_status="not_working",
            lifecycle_state="active",
            refreshed_at=datetime.now(UTC) - PROJECTION_STALE_AFTER * 4,
        )
    )
    db_session.flush()

    drift = {row["id"]: row["drift"] for row in projection_drift_rows(db_session)}

    assert drift.get(str(device.id)) == DRIFT_STALE_PROJECTION


# ── 9. Deactivating with customers attached is an admin integrity alert ──────
#
# The remaining hole in the slice: ``outage_reconcile`` sweeps only pollable
# nodes, so a device deactivated with customers still hanging off it is never
# re-examined and the stranding is invisible.
#
# The fix is deliberately NOT an outage incident. An unpolled device supports no
# reachability verdict — that is exactly why section 5 makes deactivation
# classify as UNKNOWN — so deriving "outage" from an administrative flag would
# reintroduce the same boundary violation inverted. What is raised instead is an
# admin-facing data-integrity alert, once, at the transition.


def _nas_node_with_customers(db_session, catalog_offer, name, customers):
    """A monitoring device with ``customers`` active subscriptions attached.

    Uses the NAS attachment arm ``topology.affected`` already reads (matched NAS
    -> ``Subscription.provisioning_nas_device_id``), so the alert's number is
    the same number the outage console quotes for the node.
    """
    from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
    from app.models.subscriber import Subscriber

    n = next(_ip_counter)
    nas = NasDevice(name=f"nas-{name}", management_ip=f"10.77.{n // 250}.{n % 250 + 1}")
    db_session.add(nas)
    db_session.flush()
    device = _fresh_up(
        db_session, name, matched_device_type="nas", matched_device_id=nas.id
    )
    for index in range(customers):
        subscriber = Subscriber(
            first_name="Att",
            last_name=str(index),
            email=f"{uuid.uuid4().hex}@example.com",
        )
        db_session.add(subscriber)
        db_session.flush()
        db_session.add(
            Subscription(
                subscriber_id=subscriber.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
                provisioning_nas_device_id=nas.id,
            )
        )
    db_session.flush()
    return device


def _admin_notification_target(db_session):
    """An admin the alert sink will materialize an inbox notification for."""
    from app.models.rbac import Role, SystemUserRole
    from app.models.system_user import SystemUser

    user = SystemUser(
        first_name="System",
        last_name="Admin",
        email=f"{uuid.uuid4().hex}@example.com",
    )
    role = Role(name="admin", is_active=True)
    db_session.add_all([user, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()
    return user


def _admission_alert(db_session, device):
    from app.models.admin_alert import AdminAlert
    from app.services.network_monitoring import _stranded_customer_fingerprint

    return (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == _stranded_customer_fingerprint(device))
        .one_or_none()
    )


def test_deactivating_with_customers_attached_raises_an_integrity_alert(
    db_session, catalog_offer
):
    from app.models.network_monitoring import AlertStatus

    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-1", 3)

    set_network_device_active(db_session, device, False, reason="manual_delete")

    alert = _admission_alert(db_session, device)
    assert alert is not None
    assert alert.category == "network"
    assert alert.source == "device-admission"
    assert alert.status == AlertStatus.open
    # Identity and blast radius are both in the payload.
    assert alert.details["device_id"] == str(device.id)
    assert alert.details["device_name"] == device.name
    assert alert.details["reason"] == "manual_delete"
    assert alert.details["attached_customer_count"] == 3
    assert "3" in (alert.summary or "")


def test_deactivating_an_unattached_device_stays_silent(db_session, catalog_offer):
    """A routine decommission must not fire. The silence is the feature."""
    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-0", 0)

    set_network_device_active(db_session, device, False, reason="manual_delete")

    assert device.is_active is False
    assert _admission_alert(db_session, device) is None


def test_only_active_subscriptions_count_as_attached(db_session, catalog_offer):
    """A cancelled customer behind a retired device is not a stranding."""
    from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
    from app.models.subscriber import Subscriber

    nas = NasDevice(name="nas-cancelled", management_ip="10.77.9.9")
    db_session.add(nas)
    db_session.flush()
    device = _fresh_up(
        db_session,
        "stranded-cancelled",
        matched_device_type="nas",
        matched_device_id=nas.id,
    )
    subscriber = Subscriber(
        first_name="Gone", last_name="Away", email=f"{uuid.uuid4().hex}@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.canceled,
            provisioning_nas_device_id=nas.id,
        )
    )
    db_session.flush()

    set_network_device_active(db_session, device, False, reason="manual_delete")

    assert _admission_alert(db_session, device) is None


def test_repeated_deactivation_transitions_dedupe_onto_one_alert(
    db_session, catalog_offer
):
    """Fingerprint dedupe plus the transition gate: no stacking, no re-notify.

    Scoped to this owner's fingerprint prefix, not a bare ``AdminAlert`` count.
    A global count is both too weak and too fragile for the property: it cannot
    tell "one row under a device-stable key" (correct) from "one row under a key
    that drifted between calls" (the exact failure this test exists to catch),
    and any alert another owner raised in the same session perturbs it.
    """
    from app.models.admin_alert import AdminAlert, AdminNotification
    from app.models.network_monitoring import AlertStatus
    from app.services.network_monitoring import (
        _ADMISSION_ALERT_PREFIX,
        _stranded_customer_fingerprint,
    )

    _admin_notification_target(db_session)
    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-dedupe", 2)

    set_network_device_active(db_session, device, False, reason="manual_delete")
    # Re-applying the already-inactive state is NOT an edge: the router
    # inventory sync re-asserts deactivation every cycle, and that must not
    # re-open an alert an operator already worked.
    set_network_device_active(db_session, device, False, reason="router_inventory_sync")
    set_network_device_active(db_session, device, False, reason="router_inventory_sync")

    # The harness session runs autoflush=False and the alert sink flushes the
    # alert but not the notifications it queues behind it. Production callers
    # own the commit, which flushes both; do the same here or the notification
    # query below reads an empty table and proves nothing.
    db_session.flush()

    raised = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint.like(f"{_ADMISSION_ALERT_PREFIX}%"))
        .all()
    )
    # Exactly one row, under the device-stable key, still open: three
    # transitions neither stacked rows nor re-opened the alert.
    assert [(alert.fingerprint, alert.status) for alert in raised] == [
        (_stranded_customer_fingerprint(device), AlertStatus.open)
    ]

    # One inbox notification for that alert, not one per sync cycle.
    notifications = (
        db_session.query(AdminNotification)
        .filter(AdminNotification.alert_id == raised[0].id)
        .count()
    )
    assert notifications == 1


def test_reactivation_clears_the_outstanding_alert(db_session, catalog_offer):
    from app.models.network_monitoring import AlertStatus

    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-back", 2)
    set_network_device_active(db_session, device, False, reason="manual_delete")
    assert _admission_alert(db_session, device) is not None

    set_network_device_active(db_session, device, True, reason="inventory_update")

    alert = _admission_alert(db_session, device)
    assert alert is not None
    assert alert.status == AlertStatus.resolved
    assert alert.resolved_at is not None


def test_reactivation_without_an_outstanding_alert_is_a_no_op(
    db_session, catalog_offer
):
    """Silent stays silent, and re-admission does not invent an alert.

    Reported as this owner's rows rather than a bare count so a failure names
    which of the two things happened: a row the deactivation or reactivation
    should never have created (broken zero-customer gate, or a drifting
    fingerprint that stopped the resolve finding its target), or a row left
    unresolved. A bare count cannot tell those apart.
    """
    from app.models.admin_alert import AdminAlert
    from app.services.network_monitoring import _ADMISSION_ALERT_PREFIX

    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-none", 0)
    set_network_device_active(db_session, device, False, reason="manual_delete")

    set_network_device_active(db_session, device, True, reason="inventory_update")

    db_session.flush()
    raised = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint.like(f"{_ADMISSION_ALERT_PREFIX}%"))
        .all()
    )
    assert [(alert.fingerprint, alert.status) for alert in raised] == []


def test_the_alert_never_opens_an_outage_incident(db_session, catalog_offer):
    """It is an alert, not an incident, and not a customer-visible surface."""
    from app.models.network_monitoring import OutageIncident

    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-no-inc", 4)
    before = db_session.query(OutageIncident).count()

    set_network_device_active(db_session, device, False, reason="manual_delete")

    db_session.flush()
    assert _admission_alert(db_session, device) is not None
    # Delta, not an absolute count: the property is that this transition opens
    # no incident, which must not depend on the table being empty to begin with.
    assert db_session.query(OutageIncident).count() == before


def test_a_failing_impact_read_cannot_block_the_transition(
    db_session, monkeypatch, catalog_offer
):
    """The advisory alert is subordinate to the lifecycle slice it observes."""
    import app.services.topology.affected as affected_mod

    device = _nas_node_with_customers(db_session, catalog_offer, "stranded-boom", 2)

    def _boom(*args, **kwargs):
        raise RuntimeError("graph projection exploded")

    monkeypatch.setattr(affected_mod, "affected_customers", _boom)

    set_network_device_active(db_session, device, False, reason="manual_delete")

    # Deactivation still landed, whole and correct.
    assert device.is_active is False
    assert device.live_status == "unknown"
    assert _admission_alert(db_session, device) is None


def test_the_alert_fingerprint_is_outside_every_managed_sweep_prefix(db_session):
    """A transition-scoped alert must not be auto-resolved by a sweep.

    ``resolve_missing_alerts`` closes anything under a managed prefix that is
    absent from the sweep's active set. This alert is raised at a transition and
    never appears in any sweep's active set, so it must not live under one of
    their prefixes or it would be resolved on the next evaluation run.
    """
    from app.services.admin_alerts import INFRASTRUCTURE_ALERT_PREFIX
    from app.services.credential_rotation_schedule import _INTEGRITY_FINDING_PREFIX
    from app.services.cross_app_drift import _DRIFT_ALERT_PREFIX
    from app.services.nas_lifecycle import _FINDING_PREFIX
    from app.services.network_monitoring import _ADMISSION_ALERT_PREFIX

    swept = (
        INFRASTRUCTURE_ALERT_PREFIX,
        _DRIFT_ALERT_PREFIX,
        _INTEGRITY_FINDING_PREFIX,
        _FINDING_PREFIX,
        "router-sot:",
    )
    for prefix in swept:
        assert not _ADMISSION_ALERT_PREFIX.startswith(prefix), prefix
        assert not prefix.startswith(_ADMISSION_ALERT_PREFIX), prefix
