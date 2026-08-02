"""ui.network_explorer_projection: typed search + bounded subject graphs.

The explorer restates existing owners in the shared NetworkGraphView
contract. These tests pin the boundaries: typed results with customer
identity gated, bounded neighbourhoods with explicit cohort grouping, a hard
node cap that never truncates silently, honest unknown/passive states, and
per-subject failure isolation.
"""

from __future__ import annotations

import json
import uuid

from app.models.network import OLTDevice, OntUnit, PonPort
from app.models.network_monitoring import NetworkDevice
from app.services import network_explorer as explorer
from app.services.network.forwarding_topology import ForwardingGraph
from app.services.topology import affected


def _olt(db, name="Gudu OLT"):
    olt = OLTDevice(name=name, mgmt_ip="10.0.0.2")
    db.add(olt)
    db.commit()
    db.refresh(olt)
    return olt


def _pon(db, olt, port_number=1):
    pon = PonPort(olt_id=olt.id, name=f"0/1/{port_number}", port_number=port_number)
    db.add(pon)
    db.commit()
    db.refresh(pon)
    return pon


def _ont(db, serial, *, pon_port_id=None, olt_status=None):
    ont = OntUnit(
        serial_number=serial,
        pon_port_id=pon_port_id,
        olt_status=olt_status,
    )
    db.add(ont)
    db.commit()
    db.refresh(ont)
    return ont


def _device(db, name, *, role="access"):
    device = NetworkDevice(name=name, role=role, is_active=True)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


# --- typed search ----------------------------------------------------------


def test_search_returns_typed_results_per_kind(db_session, subscriber):
    _ont(db_session, "UBNT58508c30")
    _olt(db_session, name="UBNT-OLT")

    results = explorer.search_explorer_subjects(
        db_session, "UBNT", include_customer_identity=True
    )

    kinds = {result.kind for result in results}
    assert "ont" in kinds
    assert "olt" in kinds
    ont_hit = next(result for result in results if result.kind == "ont")
    assert ont_hit.subject.startswith("ont:")
    assert ont_hit.subject_url.startswith("/admin/network/explorer?subject=ont:")
    assert ont_hit.kind_label == "ONT"


def test_search_hides_customer_identity_without_permission(db_session, subscriber):
    results_with = explorer.search_explorer_subjects(
        db_session, "Test", include_customer_identity=True
    )
    results_without = explorer.search_explorer_subjects(
        db_session, "Test", include_customer_identity=False
    )

    assert any(result.kind == "subscriber" for result in results_with)
    assert not any(
        result.kind in ("subscriber", "subscription", "radio")
        for result in results_without
    )


def test_search_escapes_like_wildcards(db_session):
    _ont(db_session, "PLAIN-1")

    results = explorer.search_explorer_subjects(
        db_session, "%", include_customer_identity=False
    )

    assert not any(result.kind == "ont" for result in results)


# --- subject views ---------------------------------------------------------


def test_customer_subject_refused_without_identity_permission(db_session):
    context = explorer.build_explorer_context(
        db_session,
        subject=f"subscription:{uuid.uuid4()}",
        query=None,
        include_customer_identity=False,
    )

    assert context.view is None
    assert context.subject_missing is True


def test_unknown_and_malformed_subjects_are_missing_not_errors(db_session):
    for subject in ("device:not-a-uuid", f"nothing:{uuid.uuid4()}", "junk"):
        context = explorer.build_explorer_context(
            db_session,
            subject=subject,
            query=None,
            include_customer_identity=True,
        )
        assert context.view is None
        assert context.subject_missing is True


def test_pon_subject_shows_onts_with_honest_states(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt)
    _ont(db_session, "ONT-UP", pon_port_id=pon.id, olt_status="online")
    _ont(db_session, "ONT-NOSTATE", pon_port_id=pon.id)

    view = explorer.build_explorer_view(db_session, f"pon_port:{pon.id}")

    by_label = {node.label: node for node in view.nodes}
    assert by_label["ONT-UP"].state == "up"
    # The projection restates the owner's word for a never-seen ONT
    # (offline, reason never_seen_retry_pending) instead of re-deciding it.
    assert by_label["ONT-NOSTATE"].state == "down"
    assert "never_seen" in by_label["ONT-NOSTATE"].tooltip
    # The PON itself is identity-only in this projection.
    pon_node = next(node for node in view.nodes if node.kind == "pon_port")
    assert pon_node.state == "unknown"
    olt_node = next(node for node in view.nodes if node.kind == "olt")
    assert olt_node.state == "not_applicable"
    assert olt_node.presentation.label == "Passive"


def test_pon_subject_groups_large_ont_fanout(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt)
    for index in range(explorer.GROUP_THRESHOLD + 5):
        _ont(db_session, f"ONT-{index:03d}", pon_port_id=pon.id)

    view = explorer.build_explorer_view(db_session, f"pon_port:{pon.id}")

    cohort = next(node for node in view.nodes if node.kind == "cohort")
    assert "+5 more ONTs" in cohort.label
    assert cohort.href.startswith("/admin/network/onts?olt_id=")
    ont_nodes = [node for node in view.nodes if node.kind == "ont"]
    assert len(ont_nodes) == explorer.GROUP_THRESHOLD


def test_ont_subject_without_assignment_walks_pon_and_olt(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt)
    ont = _ont(db_session, "LONELY-ONT", pon_port_id=pon.id)

    view = explorer.build_explorer_view(db_session, f"ont:{ont.id}")

    kinds = [node.kind for node in view.nodes]
    assert kinds == ["ont", "pon_port", "olt"]
    assert all(edge.kind == "access" for edge in view.edges)


def test_device_subject_restates_forwarding_adjacency(db_session, monkeypatch):
    core = _device(db_session, "Core-1", role="core")
    access = _device(db_session, "Access-1", role="access")
    leaf = _device(db_session, "Leaf-1", role="edge")
    graph = ForwardingGraph(
        report_sha256="stub",
        adjacency={
            core.id: frozenset({access.id}),
            access.id: frozenset({leaf.id}),
        },
        upstream_by_downstream={access.id: core.id, leaf.id: access.id},
        declaration_by_downstream={},
        root_device_ids=frozenset({core.id}),
        declaration_ids=(),
    )
    monkeypatch.setattr(affected, "forwarding_graph_projection", lambda _db: graph)

    view = explorer.build_explorer_view(db_session, f"device:{access.id}")

    labels = {node.label for node in view.nodes if node.kind == "network_device"}
    assert labels == {"Core-1", "Access-1", "Leaf-1"}
    assert all(edge.kind in ("forwarding", "access") for edge in view.edges)
    # Device nodes carry the owner's binary verdict vocabulary.
    subject_node = next(n for n in view.nodes if n.label == "Access-1")
    assert subject_node.state in ("working", "not_working", "unknown")
    assert subject_node.evidence.owner == "network.device_state"


def test_pop_site_containment_is_not_connectivity(db_session):
    from app.models.network_monitoring import PopSite

    site = PopSite(name="Jabi POP", code="JBI")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    device = _device(db_session, "Jabi-Switch")
    device.pop_site_id = site.id
    db_session.commit()

    view = explorer.build_explorer_view(db_session, f"pop_site:{site.id}")

    assert all(edge.kind == "containment" for edge in view.edges)
    site_node = next(node for node in view.nodes if node.kind == "pop")
    assert site_node.state == "not_applicable"


# --- bounds ----------------------------------------------------------------


def test_hard_node_cap_appends_explicit_overflow():
    nodes = [
        explorer._identity_node(f"n:{index}", "network_device", f"D{index}")
        for index in range(explorer.MAX_GRAPH_NODES + 40)
    ]

    capped = explorer._enforce_node_cap(nodes)

    assert len(capped) == explorer.MAX_GRAPH_NODES
    assert capped[-1].kind == "cohort"
    assert "+41 more" in capped[-1].label


def test_view_json_safety(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt)
    _ont(db_session, "JSON-ONT", pon_port_id=pon.id)

    view = explorer.build_explorer_view(db_session, f"pon_port:{pon.id}")

    payload = json.dumps(view.to_dict())
    assert "schema_version" in payload


# --- inspector -------------------------------------------------------------


def test_device_inspector_composes_verdict_impact_and_incidents(db_session):
    from app.models.network_monitoring import OutageIncident

    device = _device(db_session, "Inspect-1", role="access")
    incident = OutageIncident(
        root_node_id=device.id,
        status="confirmed",
        declared_by="classifier",
    )
    db_session.add(incident)
    db_session.commit()

    inspector = explorer.build_inspector(
        db_session, f"device:{device.id}", include_customer_identity=True
    )

    assert inspector.kind == "device"
    assert inspector.label == "Inspect-1"
    # The binary owner vocabulary, never a template-derived word.
    assert inspector.state_presentation.value in ("working", "not_working")
    assert inspector.state_reason
    assert inspector.affected_count == 0
    assert len(inspector.incidents) == 1
    assert inspector.incidents[0].status == "confirmed"
    assert inspector.incidents[0].presentation.tone.value in (
        "negative",
        "warning",
    )
    assert inspector.href == f"/admin/network/core-devices/{device.id}"
    assert inspector.href_permission == "network:device:read"


def test_ont_inspector_carries_optical_measurements_and_customer_link(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt)
    ont = _ont(db_session, "INSP-ONT", pon_port_id=pon.id, olt_status="online")
    ont.onu_rx_signal_dbm = -21.5
    ont.onu_tx_signal_dbm = 2.4
    db_session.commit()

    inspector = explorer.build_inspector(
        db_session, f"ont:{ont.id}", include_customer_identity=True
    )

    assert inspector.state_presentation.value == "up"
    displays = {m.label: m.display for m in inspector.measurements}
    assert displays["ONT receive power"] == "-21.5 dBm"
    assert displays["ONT transmit power"] == "2.4 dBm"
    # No assignment: no customer link is invented.
    assert inspector.customer360_href is None


def test_inspector_refuses_customer_subjects_without_identity(db_session):
    assert (
        explorer.build_inspector(
            db_session,
            f"subscription:{uuid.uuid4()}",
            include_customer_identity=False,
        )
        is None
    )


def test_inspector_handles_unknown_subjects(db_session):
    assert (
        explorer.build_inspector(
            db_session, "device:not-a-uuid", include_customer_identity=True
        )
        is None
    )
    assert (
        explorer.build_inspector(
            db_session, f"device:{uuid.uuid4()}", include_customer_identity=True
        )
        is None
    )


def test_pon_inspector_counts_are_bounded_queries(db_session):
    olt = _olt(db_session)
    pon = _pon(db_session, olt)
    _ont(db_session, "CNT-1", pon_port_id=pon.id, olt_status="online")
    _ont(db_session, "CNT-2", pon_port_id=pon.id)

    inspector = explorer.build_inspector(
        db_session, f"pon_port:{pon.id}", include_customer_identity=True
    )

    facts = {fact.label: fact.display for fact in inspector.facts}
    assert facts["ONTs"] == "2"
    assert facts["ONTs online"] == "1"


# --- fibre, geographic, utilization layers ---------------------------------


def test_splitter_subject_walks_to_its_fdh(db_session):
    from app.models.network import FdhCabinet, Splitter

    fdh = FdhCabinet(name="FDH-12", code="F12")
    db_session.add(fdh)
    db_session.commit()
    splitter = Splitter(name="SPL-12", fdh_id=fdh.id, splitter_ratio="1:8")
    db_session.add(splitter)
    db_session.commit()
    db_session.refresh(splitter)

    view = explorer.build_explorer_view(db_session, f"splitter:{splitter.id}")

    kinds = [node.kind for node in view.nodes]
    assert kinds == ["splitter", "fdh"]
    assert view.edges[0].kind == "containment"

    inspector = explorer.build_inspector(
        db_session, f"splitter:{splitter.id}", include_customer_identity=False
    )
    facts = {fact.label: fact for fact in inspector.facts}
    assert facts["Ratio"].display == "1:8"
    assert facts["Map"].href == "/admin/network/fiber-map"
    assert facts["Map"].href_permission == "network:fiber:read"


def test_device_inspector_composes_link_utilization(db_session, monkeypatch):
    from app.services import network_topology

    device = _device(db_session, "Util-1")
    monkeypatch.setattr(
        network_topology,
        "node_summary",
        lambda _db, _id: {
            "links": [
                {
                    "target_device": "Core-1",
                    "utilization_pct": 62.0,
                    "capacity_bps": 1_000_000_000,
                },
                {
                    "target_device": "Leaf-9",
                    "utilization_pct": None,
                    "capacity_bps": None,
                },
            ]
        },
    )

    inspector = explorer.build_inspector(
        db_session, f"device:{device.id}", include_customer_identity=False
    )

    displays = {fact.label: fact.display for fact in inspector.facts}
    assert displays["Link · Core-1"] == "62% of 1000 Mbps"
    assert displays["Link · Leaf-9"] == "— of unknown capacity"


def test_pop_site_inspector_links_existing_map(db_session):
    from app.models.network_monitoring import PopSite

    site = PopSite(name="Map POP", code="MAP")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    inspector = explorer.build_inspector(
        db_session, f"pop_site:{site.id}", include_customer_identity=False
    )

    map_fact = next(fact for fact in inspector.facts if fact.label == "Map")
    assert map_fact.href == "/admin/network/map"
    assert map_fact.href_permission == "network:map:read"


# --- coverage and drift ----------------------------------------------------


def test_coverage_is_calculated_per_subscription(db_session, subscriber, catalog_offer):
    from app.models.catalog import BillingMode, SubscriptionStatus
    from app.schemas.catalog import SubscriptionCreate
    from app.services import catalog as catalog_service

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(account_id=subscriber.id, offer_id=catalog_offer.id),
    )
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    coverage = explorer.build_network_coverage(db_session)

    assert coverage.active_subscriptions == 1
    # No ONT/NAS/basestation resolution exists, so the path is honestly
    # incomplete and lands in a gap bucket rather than being invented.
    assert coverage.complete_paths == 0
    assert coverage.coverage_percent == 0.0
    assert sum(count for _, count in coverage.gap_counts) == 1
    assert sum(m.total for m in coverage.by_medium) == 1

    gaps_metric = next(
        metric for metric in coverage.metrics if metric.key == "subscription_gaps"
    )
    assert gaps_metric.count == 1
    assert gaps_metric.presentation.value == "needs_review"
    assert gaps_metric.presentation.tone.value == "warning"
    assert gaps_metric.href == "/admin/network/topology-gaps"


def test_coverage_with_no_subscriptions_is_unknown_not_perfect(db_session):
    coverage = explorer.build_network_coverage(db_session)

    assert coverage.active_subscriptions == 0
    assert coverage.coverage_percent is None


def test_orphan_device_and_radio_queue_worklists(db_session):
    from datetime import UTC, datetime, timedelta

    from app.models.support import Ticket

    _device(db_session, "Orphan-1")
    ticket = Ticket(
        title="Unmatched radio",
        ticket_type="unmatched_radio",
        status="open",
    )
    db_session.add(ticket)
    db_session.commit()
    ticket.created_at = datetime.now(UTC) - timedelta(days=3)
    db_session.commit()

    coverage = explorer.build_network_coverage(db_session)
    metrics = {metric.key: metric for metric in coverage.metrics}

    assert metrics["orphan_devices"].count == 1
    radio_queue = metrics["unmatched_radio_queue"]
    assert radio_queue.count == 1
    assert "day(s)" in radio_queue.detail
    assert radio_queue.href == explorer.UNMATCHED_RADIO_QUEUE_HREF


def test_zero_worklists_present_as_clear(db_session):
    coverage = explorer.build_network_coverage(db_session)

    clear = [m for m in coverage.metrics if m.count == 0]
    assert clear, "expected at least one empty worklist in an empty database"
    assert all(m.presentation.value == "clear" for m in clear)
    assert all(m.presentation.tone.value == "positive" for m in clear)
