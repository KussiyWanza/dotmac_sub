"""Device admission is a single owned transition, and it stays that way.

``network.monitoring_inventory`` owns the ``NetworkDevice`` admission
lifecycle. Every deactivation must go through ``set_network_device_active`` so
the derived reachability cache is decayed with it; a caller that writes the
flag directly reintroduces the frozen-``up`` defect (a device that leaves the
poll sweep keeps asserting reachability forever, which vetoes outage detection
for every customer behind it).

These are static checks: they fail when a new caller reaches around the owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP = PROJECT_ROOT / "app"

OWNER_MODULE = "app/services/network_monitoring.py"
TRANSITION = "set_network_device_active"


def _source(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def _mentions_network_device(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == "NetworkDevice"
        for child in ast.walk(node)
    )


def _network_device_bindings(tree: ast.AST) -> set[str]:
    """Names in this module that provably hold a ``NetworkDevice``.

    Three provable origins — assigned from an expression naming the model
    (``db.get(NetworkDevice, ...)``, ``select(NetworkDevice)``, ...), annotated
    as one, or a parameter annotated as one. Other models that happen to expose
    ``is_active`` never bind these names, so the scan below cannot confuse a
    ``NasDevice``/``OntUnit``/``GeoLocation`` write for a monitoring-device one.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _mentions_network_device(node.value):
            bound.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif (
            isinstance(node, ast.AnnAssign)
            and _mentions_network_device(node.annotation)
            and isinstance(node.target, ast.Name)
        ):
            bound.add(node.target.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in [*node.args.args, *node.args.kwonlyargs]:
                if arg.annotation is not None and _mentions_network_device(
                    arg.annotation
                ):
                    bound.add(arg.arg)
    return bound


def test_only_the_owner_module_writes_network_device_is_active() -> None:
    """No service outside the owner reassigns a NetworkDevice's admission flag.

    Constructor keyword arguments are fine — creation is not a transition. It
    is reassigning an existing row that must route through the owner, because
    only the owner also decays the derived reachability cache.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        if relative == OWNER_MODULE or "NetworkDevice" not in source:
            continue
        tree = ast.parse(source)
        bound = _network_device_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "is_active"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in bound
                ):
                    offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], (
        "these callers write NetworkDevice.is_active directly instead of "
        f"requesting {TRANSITION}: {offenders}"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "app/services/router_management/inventory.py",
        "app/services/monitoring_metrics.py",
        "app/services/web_network_monitoring.py",
        "app/services/web_network_core_devices_forms.py",
        "app/services/network/olt_web_forms.py",
    ],
)
def test_known_admission_callers_request_the_owner_transition(relative: str) -> None:
    assert TRANSITION in _source(relative)


def _function_named(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_router_field_sync_does_not_copy_admission_as_a_field() -> None:
    """The cross-domain write this slice removed.

    ``device.is_active = router.is_active`` sat in a field-copy helper: a
    router_management writer performing a monitoring-inventory lifecycle
    transition, skipping the decay. Router inventory remains an authoritative
    INPUT to that admission, but it requests the transition.

    Asserted over the AST, not the raw text: the helper's docstring names the
    removed line to explain *why* the boundary moved, and that explanation is
    worth keeping. What is forbidden is the assignment, not the mention.
    """
    tree = ast.parse(_source("app/services/monitoring_metrics.py"))
    helper = _function_named(tree, "_sync_router_fields_to_device")

    assignments = [
        node.lineno
        for node in ast.walk(helper)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "is_active"
    ]
    assert assignments == [], (
        "the router field sync assigns admission directly instead of "
        f"requesting {TRANSITION} (lines {assignments})"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == TRANSITION
        for node in ast.walk(helper)
    )


def test_the_transition_decays_the_derived_reachability_cache() -> None:
    source = _source(OWNER_MODULE)
    assert "device.live_status = _INACTIVE_LIVE_STATUS" in source


def test_the_transition_raises_the_stranded_customer_integrity_alert() -> None:
    """Deactivating with customers attached must announce itself.

    ``outage_reconcile`` sweeps only pollable nodes, so a device deactivated
    with customers still on it is never re-examined. The owned transition is the
    only place that observes the deactivation edge, so the alert hangs there.
    """
    tree = ast.parse(_source(OWNER_MODULE))
    transition = _function_named(tree, TRANSITION)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_sync_stranded_customer_alert"
        for node in ast.walk(transition)
    ), f"{TRANSITION} no longer raises the stranded-customer integrity alert"


def test_the_admission_alert_is_never_an_outage_incident() -> None:
    """The explicitly rejected design, pinned so it cannot creep back.

    An unpolled device supports NO reachability verdict — that is why this slice
    makes deactivation classify as ``UNKNOWN`` rather than ``NODE_OUTAGE``.
    Deriving a customer-facing outage from an administrative flag would
    reintroduce the same boundary violation inverted, and would fire on every
    routine decommission. The admission owner therefore never touches
    ``OutageIncident``, and the outage sweep is not widened to inactive nodes.
    """
    owner = ast.parse(_source(OWNER_MODULE))
    incident_refs = [
        node.lineno
        for node in ast.walk(owner)
        if isinstance(node, ast.Name) and node.id == "OutageIncident"
    ]
    assert incident_refs == [], (
        "the admission owner references OutageIncident — an administrative flag "
        f"must not decide a customer-facing outage (lines {incident_refs})"
    )

    sweep = _source("app/services/topology/outage_reconcile.py")
    assert "NetworkDevice.is_active.is_(True)" in sweep, (
        "the outage sweep was widened to inactive nodes; inventory absence is "
        "not evidence of unreachability"
    )


def test_device_derivation_does_not_filter_out_inactive_devices() -> None:
    """Filtering here made the projection reconciler DELETE the device."""
    source = _source("app/services/web_network_core_devices_inventory.py")
    assert "for device in monitoring_devices if device.is_active" not in source
    assert "lifecycle_state" in source


def test_projection_carries_the_inactive_never_working_release_gate() -> None:
    model = _source("app/models/network_monitoring.py")
    assert "ck_device_projection_inactive_never_working" in model
    assert "lifecycle_state = 'active' OR operational_status = 'not_working'" in model

    reconciler = _source("app/services/device_projection_reconcile.py")
    assert "_gated_status" in reconciler


def test_customer_facing_readers_apply_the_freshness_gate() -> None:
    """A dead warmer must not be able to serve a confident stale verdict."""
    classifier = _source("app/services/topology/health_classifier.py")
    assert "trusted_live_status" in classifier
    # The raw column must not be read straight into the mgmt-plane decision.
    assert '_mgmt_state(getattr(node, "live_status", None))' not in classifier

    for relative in (
        "app/services/topology/connection_status.py",
        "app/services/topology/last_mile.py",
    ):
        assert "warm_stale" in _source(relative), relative


def test_every_classifier_call_site_supplies_the_dead_man_reading() -> None:
    """``warm_stale`` is evidence the caller must supply, not a default.

    The gate decays a positive only on positive evidence, and an omitted
    ``warm_stale`` reads as "no evidence" — deliberately, so an unhydrated row
    is never mistaken for a stale one. The cost is that a caller which forgets
    to pass it silently loses the dead-man switch, so every call site is
    checked here instead.
    """
    missing: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"classify_node", "localize_outage"}
                and not any(kw.arg == "warm_stale" for kw in node.keywords)
            ):
                missing.append(f"{relative}:{node.lineno} {node.func.id}")
    assert missing == [], (
        "these classifier calls omit the warmer dead-man reading, so a frozen "
        f"live_status can still decide their verdict: {missing}"
    )


def test_retired_zabbix_provenance_module_is_gone() -> None:
    """The dead drift filter keyed on a source string from a deleted importer.

    Precise about what is forbidden: the retired provenance must not appear as
    *executable* code in the drift report — no string literal, no import of the
    deleted module, no comparison against ``NetworkDevice.source``. Comments
    explaining the retirement are the point of the change and stay.
    """
    assert not (PROJECT_ROOT / "app/services/topology/sources.py").exists()

    tree = ast.parse(_source("app/services/topology/gaps.py"))

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "zabbix_reconcile" not in literals

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.services.topology.sources"
            assert all(alias.name != "RECONCILED_SOURCE" for alias in node.names)

    source_comparisons = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "source"
    ]
    assert source_comparisons == [], (
        f"the drift report still filters on provenance (lines {source_comparisons})"
    )


def test_ownership_is_recorded_in_the_map_and_registry() -> None:
    owner = service_relationship("network.monitoring_inventory")
    assert "monitoring device admission lifecycle transitions" in owner.owns
    assert owner.notes is not None
    assert TRANSITION in owner.notes

    relationship_map = _source("docs/SOT_RELATIONSHIP_MAP.md")
    assert TRANSITION in relationship_map
    assert "admission" in relationship_map

    # The rejected alternative is recorded, not just the chosen one. Both
    # documents wrap their prose, so compare on normalised whitespace.
    for document in (owner.notes, relationship_map):
        prose = " ".join(document.split())
        assert "data-integrity alert" in prose
        assert "Inventory absence must not open a customer-facing outage" in prose
