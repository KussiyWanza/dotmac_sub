"""Unmatched-radio ops queue: dedupe, auto-close, placeholder retirement.

Also asserts the uisp_sync ADOPTION contract: the sync's station upsert adopts
a pre-created manual cpe_devices row (matched by normalized MAC, uisp_device_id
IS NULL) in place instead of creating a duplicate. The hook lives in
app/services/topology/uisp_sync.py (``_adoption_candidates``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.audit import AuditEvent
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.network import CPEDevice, DeviceStatus, DeviceType
from app.models.network_monitoring import NetworkDevice
from app.models.notification import Notification
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.support import Ticket, TicketStatus
from app.services import radio_registration, support, unmatched_radio_queue

MAC = "24:A4:3C:AA:BB:01"
MAC_COMPACT = "24a43caabb01"


def _active_subscription(db_session, subscriber, catalog_offer, mac):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        mac_address=mac,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _manual_radio(db_session, subscriber, mac=MAC, age_hours=0):
    row = CPEDevice(
        subscriber_id=subscriber.id,
        device_type=DeviceType.wireless_radio,
        mac_address=mac,
    )
    db_session.add(row)
    db_session.flush()
    if age_hours:
        row.created_at = datetime.now(UTC) - timedelta(hours=age_hours)
        db_session.flush()
    return row


def _confirmed_radio(db_session, subscriber, mac=MAC):
    row = CPEDevice(
        subscriber_id=subscriber.id,
        device_type=DeviceType.wireless_radio,
        mac_address=mac,
        uisp_device_id=str(uuid.uuid4()),
    )
    db_session.add(row)
    db_session.flush()
    return row


class TestQueueSemantics:
    def test_open_item_dedupes_per_mac(self, db_session):
        first, created_first = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="t",
            description="d",
        )
        second, created_second = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="t",
            description="d",
        )

        assert created_first is True
        assert created_second is False
        assert second.id == first.id
        assert second.metadata_["occurrences"] == 2
        assert (
            db_session.query(Ticket)
            .filter(Ticket.ticket_type == unmatched_radio_queue.TICKET_TYPE)
            .count()
            == 1
        )

    def test_open_item_is_a_numbered_audited_silent_internal_ticket(
        self, db_session, monkeypatch
    ):
        def _unexpected_consequence(*args, **kwargs):
            raise AssertionError("silent internal ticket ran standard consequences")

        monkeypatch.setattr(
            support.Tickets, "_apply_sla_policy", _unexpected_consequence
        )
        monkeypatch.setattr(
            support.Tickets, "_apply_automation_rules", _unexpected_consequence
        )
        monkeypatch.setattr(
            support.Tickets,
            "_queue_notifications_for_assignments",
            _unexpected_consequence,
        )

        ticket, _ = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="Registered radio not seen by UISP",
            description="d",
        )
        assert ticket.status == TicketStatus.open.value
        assert ticket.number
        assert unmatched_radio_queue.TAG in (ticket.tags or [])
        assert ticket.metadata_["radio_mac"] == MAC_COMPACT
        assert db_session.query(Notification).count() == 0

        audit = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action == "create",
                AuditEvent.entity_type == "support_ticket",
                AuditEvent.entity_id == str(ticket.id),
            )
            .one()
        )
        assert audit.actor_id == unmatched_radio_queue.__name__.rsplit(".", 1)[-1]
        assert audit.metadata_["number"] == ticket.number
        assert audit.metadata_["creation_consequence_mode"] == "silent_internal"

        event = (
            db_session.query(EventStore)
            .filter(
                EventStore.event_type == "custom",
                EventStore.payload["ticket_id"].as_string() == str(ticket.id),
            )
            .one()
        )
        assert event.payload["name"] == "ticket.created"
        assert event.payload["ticket_number"] == ticket.number
        assert event.payload["creation_consequence_mode"] == "silent_internal"

    def test_open_item_assigns_network_support_team_when_present(self, db_session):
        team = ServiceTeam(
            name=unmatched_radio_queue.NETWORK_SUPPORT_TEAM_NAME,
            team_type=ServiceTeamType.support.value,
            is_active=True,
        )
        db_session.add(team)
        db_session.flush()

        ticket, _ = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="Registered radio not seen by UISP",
            description="d",
        )

        assert ticket.service_team_id == team.id

    def test_repeat_observation_repairs_legacy_missing_number(self, db_session):
        legacy = Ticket(
            title="Legacy unmatched radio",
            status=TicketStatus.open.value,
            ticket_type=unmatched_radio_queue.TICKET_TYPE,
            metadata_={
                "opened_by": "unmatched_radio_queue",
                "radio_mac": MAC_COMPACT,
                "occurrences": 1,
            },
        )
        db_session.add(legacy)
        db_session.flush()
        assert legacy.number is None

        observed, created = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="ignored for dedupe",
            description="ignored for dedupe",
        )

        assert created is False
        assert observed.id == legacy.id
        assert observed.number
        assert observed.metadata_["occurrences"] == 2
        repair_audit = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action == "number_repair",
                AuditEvent.entity_type == "support_ticket",
                AuditEvent.entity_id == str(legacy.id),
            )
            .one()
        )
        assert repair_audit.metadata_["number"] == observed.number

    def test_closed_item_allows_a_new_one_later(self, db_session):
        ticket, _ = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="t",
            description="d",
        )
        unmatched_radio_queue.close_item(db_session, ticket, "done")
        assert ticket.status == TicketStatus.closed.value
        assert ticket.closed_at is not None

        _, created = unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="t",
            description="d",
        )
        assert created is True


class TestEvaluate:
    def test_auto_closes_when_radio_becomes_matched(self, db_session, subscriber):
        unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_NOT_ADOPTED,
            title="t",
            description="d",
        )
        _confirmed_radio(db_session, subscriber)

        stats = unmatched_radio_queue.evaluate(db_session)

        assert stats.get("closed_matched") == 1
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None

    def test_auto_closes_conflict_when_single_owner_remains(
        self, db_session, subscriber, catalog_offer
    ):
        unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_CONFLICT,
            title="t",
            description="d",
        )
        # Only ONE subscriber now claims the MAC -> conflict cleared.
        _active_subscription(db_session, subscriber, catalog_offer, MAC)

        stats = unmatched_radio_queue.evaluate(db_session)

        assert stats.get("closed_conflict_cleared") == 1
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None

    def test_ap_unresolved_item_stays_open_until_parented(self, db_session, subscriber):
        # A confirmed-but-unparented row must NOT close an ap_unresolved item;
        # only gaining a parent AP node does.
        unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_AP_UNRESOLVED,
            title="t",
            description="d",
        )
        row = _confirmed_radio(db_session, subscriber)

        stats = unmatched_radio_queue.evaluate(db_session)
        assert stats.get("closed_ap_resolved") is None
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is not None

        node = NetworkDevice(name=f"ap-{uuid.uuid4().hex[:6]}")
        db_session.add(node)
        db_session.flush()
        row.parent_network_device_id = node.id
        db_session.flush()

        stats = unmatched_radio_queue.evaluate(db_session)
        assert stats.get("closed_ap_resolved") == 1
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None

    def test_uisp_unmatched_item_closes_when_confirmed(self, db_session, subscriber):
        unmatched_radio_queue.open_item(
            db_session,
            mac_compact=MAC_COMPACT,
            reason=unmatched_radio_queue.REASON_UISP_UNMATCHED,
            title="t",
            description="d",
        )
        _confirmed_radio(db_session, subscriber)

        stats = unmatched_radio_queue.evaluate(db_session)

        assert stats.get("closed_matched") == 1
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None

    def test_opens_item_for_stale_unadopted_registration(self, db_session, subscriber):
        _manual_radio(db_session, subscriber, age_hours=48)

        stats = unmatched_radio_queue.evaluate(db_session)

        assert stats.get("opened_not_adopted") == 1
        item = unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT)
        assert item is not None
        assert item.subscriber_id == subscriber.id
        assert item.metadata_["reason"] == unmatched_radio_queue.REASON_NOT_ADOPTED

    def test_fresh_registration_gets_grace_period(self, db_session, subscriber):
        _manual_radio(db_session, subscriber, age_hours=0)

        stats = unmatched_radio_queue.evaluate(db_session)

        assert stats.get("opened_not_adopted") is None
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None

    def test_repeated_runs_do_not_spam(self, db_session, subscriber):
        _manual_radio(db_session, subscriber, age_hours=48)

        unmatched_radio_queue.evaluate(db_session)
        unmatched_radio_queue.evaluate(db_session)

        tickets = (
            db_session.query(Ticket)
            .filter(Ticket.ticket_type == unmatched_radio_queue.TICKET_TYPE)
            .all()
        )
        assert len(tickets) == 1

    def test_retires_placeholder_superseded_by_sync_row(self, db_session, subscriber):
        manual = _manual_radio(db_session, subscriber, age_hours=48)
        _confirmed_radio(db_session, subscriber)

        stats = unmatched_radio_queue.evaluate(db_session)

        db_session.refresh(manual)
        assert stats.get("placeholders_retired") == 1
        assert manual.status == DeviceStatus.retired
        assert "Superseded by UISP-synced device" in (manual.notes or "")
        # No "not adopted" item for a radio the sync has confirmed.
        assert unmatched_radio_queue.find_open_item(db_session, MAC_COMPACT) is None

    def test_placeholder_owned_by_other_subscriber_is_not_retired(
        self, db_session, subscriber
    ):
        from app.models.subscriber import Subscriber

        other = Subscriber(
            first_name="Other",
            last_name="Customer",
            email=f"other-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(other)
        db_session.flush()
        manual = _manual_radio(db_session, other, age_hours=48)
        _confirmed_radio(db_session, subscriber)

        unmatched_radio_queue.evaluate(db_session)

        db_session.refresh(manual)
        assert manual.status == DeviceStatus.active


class TestBeatRegistration:
    def test_task_is_registered_with_celery(self):
        import app.tasks  # noqa: F401 - triggers task registration
        from app.celery_app import celery_app

        assert (
            "app.tasks.unmatched_radio.run_unmatched_radio_review" in celery_app.tasks
        )

    def test_seed_migration_targets_the_registered_task_name(self):
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "211_seed_unmatched_radio_review_task.py"
        )
        spec = importlib.util.spec_from_file_location("migration_211_seed", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        assert (
            migration.TASK_PATH
            == "app.tasks.unmatched_radio.run_unmatched_radio_review"
        )
        assert migration.TASK_NAME == "topology_unmatched_radio_review"


# ---------------------------------------------------------------------------
# Follow-up contract: uisp_sync adoption of pre-registered rows
# ---------------------------------------------------------------------------


def _station_payload(mac=MAC):
    """Minimal UISP station payload (shape mirrors tests/test_uisp_topology_sync)."""
    return [
        {
            "identification": {
                "id": "b1b1b1b1-1111-2222-3333-555555555555",
                "name": "CUST-JOHN-DOE",
                "model": "LBE-5AC-Gen2",
                "mac": mac,
                "role": "station",
                "type": "airMax",
                "site": {"id": "site-endpoint-1", "name": "Site", "type": "endpoint"},
            },
            "ipAddress": "10.20.30.40",
            "overview": {"status": "active"},
            "attributes": None,
        }
    ]


class _FakeUispClient:
    def __init__(self, devices):
        self.devices = devices

    def list_devices(self):
        return self.devices

    def list_sites(self):
        return []

    def list_airmax_stations(self, ap_id):
        return []

    def list_olt_onus(self, olt_id):
        return []

    def list_data_links(self):
        return []


def test_uisp_sync_adopts_preregistered_row(db_session, subscriber, catalog_offer):
    from app.services.topology.uisp_sync import sync

    # Install-time registration: manual cpe row + stamped subscription MAC.
    subscription = _active_subscription(db_session, subscriber, catalog_offer, None)
    radio_registration.register_radio_mac(
        db_session, subscription_id=str(subscription.id), mac=MAC
    )

    sync(db_session, _FakeUispClient(_station_payload()))

    rows = (
        db_session.query(CPEDevice)
        .filter(CPEDevice.subscriber_id == subscriber.id)
        .all()
    )
    # ADOPTION CONTRACT: exactly one row, upgraded in place with the UISP id.
    assert len(rows) == 1
    assert rows[0].uisp_device_id == "b1b1b1b1-1111-2222-3333-555555555555"
