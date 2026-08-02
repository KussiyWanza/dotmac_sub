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
