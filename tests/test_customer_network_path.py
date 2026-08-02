"""ui.customer_network_path_projection: shared graph view + endpoint contract.

network.access_path keeps identity, ordering, and gaps; observation owners
keep state and freshness; ui.status_presentation keeps label/tone/icon
meaning. These tests pin the composition: the projection restates owner facts
verbatim, adds presentation and composed display strings, degrades per
subscription on failure, and stays inside the query budget.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event

from app.models.catalog import BillingMode
from app.schemas.catalog import SubscriptionCreate
from app.services import catalog as catalog_service
from app.services import customer_network_path as cnp
from app.services.topology.customer_path import CustomerPath

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


class _Asset(SimpleNamespace):
    pass


def _subscription_stub():
    return SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        login="10005452",
        ipv4_address="10.10.11.6",
    )


def _fiber_path(*, ont_serial: str = "UBNT58508c30", seen_at: datetime = NOW):
    return CustomerPath(
        ont=_Asset(
            id=uuid.uuid4(),
            serial_number=ont_serial,
            olt_status="online",
            olt_status_seen_at=seen_at,
            model="EchoLife HG8546M",
            onu_rx_signal_dbm=-21.5,
            olt_rx_signal_dbm=-24.0,
        ),
        pon_port=_Asset(id=uuid.uuid4(), name="0/1/3", port_number=3),
        access_device=_Asset(id=uuid.uuid4(), name="Gudu OLT"),
        access_device_kind="olt",
        upstream_chain=[_Asset(id=uuid.uuid4(), name="Abuja BNG")],
    )


def _wireless_path(*, observed_at: datetime):
    radio = _Asset(
        id=uuid.uuid4(),
        serial_number="RADIO-1",
        last_uisp_status="active",
        rf_signal_dbm=-62.0,
        rf_signal_source="uisp_ap_station",
        rf_signal_observed_at=observed_at,
    )
    ap = _Asset(id=uuid.uuid4(), name="Jabi Sector 2")
    return CustomerPath(
        radio=radio,
        access_device=ap,
        access_device_kind="ap",
        node=ap,
        basestation=_Asset(id=uuid.uuid4(), name="Jabi POP"),
        upstream_chain=[_Asset(id=uuid.uuid4(), name="Abuja BNG")],
    )


# --- graph view fidelity -------------------------------------------------


def test_fiber_view_restates_owner_identity_order_and_state():
    path = _fiber_path()
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=path
    )
    view = projection.view

    assert [node.kind for node in view.nodes] == [
        "ont",
        "pon_port",
        "olt",
        "network_device",
    ]
    ont = view.nodes[0]
    assert ont.state == "up"
    assert ont.presentation.tone.value == "positive"
    assert ont.evidence.owner == "network.olt_observed_state"
    assert ont.evidence.observed_at == NOW
    # Unenriched hops stay honestly unknown — never dressed up as up.
    assert view.nodes[1].state == "unknown"
    assert view.nodes[1].presentation.tone.value == "neutral"
    # Edges restate the owner's ordering, nothing more.
    assert [(e.source_id, e.target_id) for e in view.edges] == [
        (view.nodes[i].id, view.nodes[i + 1].id) for i in range(3)
    ]
    assert view.complete is True
    assert view.subject_kind == "subscription"


def test_ont_measurement_is_owner_composed():
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=_fiber_path()
    )
    measurements = projection.view.nodes[0].measurements

    assert len(measurements) == 1
    assert measurements[0].label == "ONT receive power"
    assert measurements[0].display == "-21.5 dBm"
    assert measurements[0].unit == "dBm"


def test_view_to_dict_is_json_safe():
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=_fiber_path()
    )

    payload = json.dumps(projection.view_dict)

    assert "schema_version" in payload


def test_upstream_unproven_break_becomes_a_typed_gap():
    path = _fiber_path()
    path.upstream_chain = []
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=path
    )
    view = projection.view

    assert view.complete is False
    assert [gap.code for gap in view.gaps] == ["upstream.unproven"]
    gap = view.gaps[0]
    assert gap.presentation.tone.value == "warning"
    assert gap.presentation.label == "upstream.unproven"
    # The gap anchors to the last proven hop instead of floating free.
    assert gap.after_node_id == view.nodes[-1].id


def test_no_equipment_path_is_an_explicit_gap_not_an_invented_hop():
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=CustomerPath(gap="no_access_equipment")
    )
    view = projection.view

    assert view.nodes == ()
    assert {gap.code for gap in view.gaps} == {
        "path.no_access_equipment",
        "path.unresolved",
    }


# --- serving endpoint presentation ---------------------------------------


def test_fiber_endpoint_presentation_names_the_proving_record():
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=_fiber_path()
    )
    endpoint = projection.endpoint

    assert endpoint.endpoint_source == "ont_assignment"
    assert endpoint.source_presentation.label == "ONT assignment"
    assert endpoint.source_presentation.tone.value == "neutral"
    assert endpoint.partial_notice is None


def test_wireless_fresh_rf_display_is_owner_composed():
    projection = cnp.project_subscription_network_path(
        None,
        _subscription_stub(),
        path=_wireless_path(observed_at=datetime.now(UTC) - timedelta(minutes=5)),
    )
    endpoint = projection.endpoint

    assert endpoint.rf_signal_freshness == "fresh"
    assert endpoint.rf_display == "-62 dBm"
    assert endpoint.rf_observed_display.startswith("Observed at ")
    assert endpoint.rf_freshness_presentation.tone.value == "neutral"
    assert endpoint.endpoint_source == "uisp_observation"


def test_wireless_stale_rf_keeps_value_and_age_with_warning_tone():
    projection = cnp.project_subscription_network_path(
        None,
        _subscription_stub(),
        path=_wireless_path(observed_at=datetime.now(UTC) - timedelta(hours=3)),
    )
    endpoint = projection.endpoint

    assert endpoint.rf_signal_freshness == "stale"
    assert endpoint.rf_display.startswith("Stale (last -62 dBm at ")
    assert endpoint.rf_observed_display is None
    assert endpoint.rf_freshness_presentation.tone.value == "warning"


def test_cleared_observation_never_renders_as_a_current_signal():
    path = _wireless_path(observed_at=datetime.now(UTC))
    path.radio.rf_signal_dbm = None
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=path
    )

    assert projection.endpoint.rf_display == "Signal unavailable"
    assert projection.endpoint.rf_freshness_presentation.tone.value == "neutral"


def test_ap_unresolved_is_a_named_repairable_gap():
    path = CustomerPath(
        access_device=_Asset(id=uuid.uuid4(), name="Abuja BNG"),
        access_device_kind="nas",
        unparented_radio=_Asset(id=uuid.uuid4(), serial_number="RADIO-9"),
        upstream_chain=[],
    )
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=path
    )
    endpoint = projection.endpoint

    assert endpoint.radio_ap_unresolved is True
    assert "unmatched-radio queue" in endpoint.ap_unresolved_notice
    assert "RADIO-9" in endpoint.ap_unresolved_notice


# --- degradation and isolation -------------------------------------------


def test_failed_resolution_degrades_to_unresolved(monkeypatch):
    def _boom(_db, _sub):
        raise RuntimeError("topology backend unavailable")

    monkeypatch.setattr(cnp, "resolve_subscription_access_path", _boom)

    projection = cnp.project_subscription_network_path(None, _subscription_stub())

    assert projection.endpoint.endpoint_source == "unresolved"
    assert projection.endpoint.source_presentation.tone.value == "warning"
    assert projection.view is None
    assert projection.trace is None


def test_multi_subscription_projection_isolates_siblings(monkeypatch):
    sub_a = _subscription_stub()
    sub_b = _subscription_stub()
    paths = {
        sub_a.id: _fiber_path(ont_serial="ONT-A"),
        sub_b.id: _fiber_path(ont_serial="ONT-B"),
    }
    monkeypatch.setattr(
        cnp,
        "resolve_subscription_access_path",
        lambda _db, sub: paths[sub.id],
    )

    results = cnp.project_subscription_network_paths(None, [sub_a, sub_b])

    assert results[str(sub_a.id)].view.subject_id == str(sub_a.id)
    assert results[str(sub_b.id)].view.subject_id == str(sub_b.id)
    assert results[str(sub_a.id)].view.nodes[0].label == "ONT-A"
    assert results[str(sub_b.id)].view.nodes[0].label == "ONT-B"


def test_one_failing_sibling_does_not_take_the_others_down(monkeypatch):
    sub_a = _subscription_stub()
    sub_b = _subscription_stub()

    def _resolve(_db, sub):
        if sub.id == sub_b.id:
            raise RuntimeError("resolution failed")
        return _fiber_path(ont_serial="ONT-A")

    monkeypatch.setattr(cnp, "resolve_subscription_access_path", _resolve)

    results = cnp.project_subscription_network_paths(None, [sub_a, sub_b])

    assert results[str(sub_a.id)].view is not None
    assert results[str(sub_b.id)].view is None
    assert results[str(sub_b.id)].endpoint.endpoint_source == "unresolved"


# --- deep links and repair destinations ----------------------------------


def test_nodes_carry_owner_deep_links_with_permissions():
    projection = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=_fiber_path()
    )
    ont, pon, olt, upstream = projection.view.nodes

    assert ont.href == f"/admin/network/onts/{ont.asset_id}"
    assert ont.href_permission == "network:ont:read"
    # PON ports have no page; they live on their adjacent proven OLT's tab.
    assert pon.href == f"/admin/network/olts/{olt.asset_id}?tab=pon-ports"
    assert pon.href_permission == "network:olt:read"
    assert olt.href == f"/admin/network/olts/{olt.asset_id}"
    assert upstream.href == f"/admin/network/core-devices/{upstream.asset_id}"
    assert upstream.href_permission == "network:device:read"


def test_gap_repair_points_at_canonical_queues():
    path = _fiber_path()
    path.upstream_chain = []
    unproven = cnp.project_subscription_network_path(
        None, _subscription_stub(), path=path
    ).view.gaps[0]
    assert unproven.repair_href == "/admin/network/topology-gaps"
    assert unproven.repair_permission == "monitoring:read"

    sub = _subscription_stub()
    missing_ont = cnp.project_subscription_network_path(
        None, sub, path=CustomerPath(gap="no_ont")
    ).view.gaps
    ont_gap = next(gap for gap in missing_ont if gap.code == "path.no_ont")
    assert (
        ont_gap.repair_href
        == f"/admin/network/onts?assign_subscriber={sub.subscriber_id}"
    )
    assert ont_gap.repair_permission == "network:ont:read"


def test_radio_hop_carries_owner_composed_rf_measurement():
    projection = cnp.project_subscription_network_path(
        None,
        _subscription_stub(),
        path=_wireless_path(observed_at=datetime.now(UTC) - timedelta(hours=3)),
    )
    radio = next(n for n in projection.view.nodes if n.kind == "radio")
    rf = next(m for m in radio.measurements if m.name == "rf_signal_dbm")

    assert rf.display == "-62 dBm (stale)"
    assert rf.freshness == "stale"


# --- passive fibre detail --------------------------------------------------


def _fiber_trace_stub(subscription_id):
    from app.services.fiber_topology import (
        FiberSubscriptionTrace,
        FiberTraceGap,
        FiberTraceHop,
    )

    olt_id = uuid.uuid4()
    splitter_id = uuid.uuid4()
    return FiberSubscriptionTrace(
        subscription_id=subscription_id,
        customer_label="Customer Tester",
        subscription_status="active",
        hops=(
            FiberTraceHop(
                kind="olt",
                label="Gudu OLT",
                asset_id=olt_id,
                evidence="network.fiber_topology",
                operational_state="up",
            ),
            FiberTraceHop(
                kind="pon_port",
                label="0/1/3",
                asset_id=uuid.uuid4(),
                evidence="network.fiber_topology",
            ),
            FiberTraceHop(
                kind="splitter",
                label="SPL-12 1:8",
                asset_id=splitter_id,
                evidence="reviewed splice record",
                insertion_loss_db="10.5",
                cumulative_splitter_loss_db="10.5",
            ),
            FiberTraceHop(
                kind="drop_segment",
                label="Drop 88m",
                asset_id=None,
                evidence="reviewed drop record",
            ),
        ),
        gaps=(
            FiberTraceGap(
                code="active_fdh_missing",
                message="No active FDH links this splitter.",
            ),
        ),
        electronic_complete=True,
        physical_complete=False,
        upstream_scope="olt",
        upstream_message="",
    )


def test_fiber_detail_restates_owner_hops_without_fabricating_state(monkeypatch):
    from app.services import fiber_topology

    sub_id = uuid.uuid4()
    monkeypatch.setattr(
        fiber_topology,
        "trace_fiber_subscription",
        lambda _db, _sid: _fiber_trace_stub(sub_id),
    )

    view = cnp.project_subscription_fiber_detail(None, sub_id)

    olt, pon, splitter, drop = view.nodes
    assert olt.state == "up"
    # Passive plant renders identity and continuity: not-applicable stays
    # distinct from unknown and is never dressed up as up/down.
    assert splitter.state == "not_applicable"
    assert splitter.presentation.label == "Passive"
    assert splitter.href == f"/admin/network/splitters/{splitter.asset_id}"
    assert splitter.href_permission == "network:fiber:read"
    losses = [m.display for m in splitter.measurements]
    assert "10.5 dB" in losses
    # PON ports link to the adjacent proven OLT even when it precedes them.
    assert pon.href == f"/admin/network/olts/{olt.asset_id}?tab=pon-ports"
    gap = view.gaps[0]
    assert gap.code == "active_fdh_missing"
    assert gap.repair_href == (f"/admin/network/fiber-trace?subscription_id={sub_id}")
    assert gap.repair_permission == "network:fiber:read"


def test_fiber_detail_is_none_when_the_owner_cannot_trace(monkeypatch):
    from app.services import fiber_topology

    def _refuse(_db, _sid):
        raise ValueError("subscription is not a fiber service")

    monkeypatch.setattr(fiber_topology, "trace_fiber_subscription", _refuse)

    assert cnp.project_subscription_fiber_detail(None, uuid.uuid4()) is None


# --- template ownership boundary -----------------------------------------


def test_template_holds_no_path_status_to_colour_or_label_decisions():
    template = Path("templates/admin/customers/detail.html").read_text()

    for retired in (
        "node.state ==",
        "endpoint_source == 'live_session'",
        "endpoint_source == 'ont_assignment'",
        "rf_signal_freshness ==",
        "'%.0f'|format",
        # The exact retired hop-chip colour maps.
        "border-red-300 bg-red-50 text-red-700",
        "border-emerald-300 bg-emerald-50 text-emerald-700",
        # The legacy broken NAS deep link.
        '"/admin/nas/',
    ):
        assert retired not in template, retired

    # The path renders the shared contract through the single macro renderer.
    assert "card.network_path" in template
    assert "network_path_graph(card.network_path, request)" in template

    shared = Path("templates/admin/customers/_network_path.html").read_text()
    assert 'status-panel-" ~ node.presentation.tone' in shared
    assert "can(request, node.href_permission)" in shared
    assert "can(request, gap.repair_permission)" in shared


def test_new_partials_compile():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    for name in (
        "admin/customers/_network_path.html",
        "admin/customers/_fiber_path_panel.html",
    ):
        env.parse(Path("templates", name).read_text())


# --- query budget ---------------------------------------------------------


class QueryCounter:
    """Count SQL statements issued while projecting."""

    def __init__(self, db):
        self.bind = db.get_bind()
        self.count = 0

    def __enter__(self):
        event.listen(self.bind, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(self.bind, "before_cursor_execute", self._hit)

    def _hit(self, *_args, **_kwargs):
        self.count += 1


def _make_subscriber(db):
    from app.models.subscriber import Subscriber
    from app.services.subscriber import _default_reseller_id

    subscriber = Subscriber(
        first_name="Path",
        last_name="Budget",
        email=f"path-budget-{uuid.uuid4().hex[:10]}@example.test",
        reseller_id=_default_reseller_id(db),
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _make_subscription(db, subscriber, offer, login: str):
    subscription = catalog_service.subscriptions.create(
        db,
        SubscriptionCreate(account_id=subscriber.id, offer_id=offer.id),
    )
    subscription.billing_mode = BillingMode.postpaid
    subscription.login = login
    db.commit()
    return subscription


def test_projection_query_budget_slope(db_session, catalog_offer):
    """Per-subscription cost stays flat: N paths cost at most N single budgets."""

    # One active subscription per account is a catalog invariant, so the
    # cohort spans three accounts — the projection cost is per subscription
    # either way.
    subs = [
        _make_subscription(
            db_session, _make_subscriber(db_session), catalog_offer, f"1000{i}"
        )
        for i in range(3)
    ]

    with QueryCounter(db_session) as single:
        cnp.project_subscription_network_paths(db_session, subs[:1])
    with QueryCounter(db_session) as triple:
        cnp.project_subscription_network_paths(db_session, subs)

    assert triple.count <= 3 * single.count + 2, (
        f"projection query slope regressed: 1 subscription = {single.count}, "
        f"3 subscriptions = {triple.count}"
    )
    assert single.count < 40, f"single-subscription budget blew up: {single.count}"
