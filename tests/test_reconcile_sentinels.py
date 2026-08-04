"""Unset sentinels must never reach a device.

Three layers substitute defaults for missing ONT configuration — the composer
(``resolve_effective_ont_config``), the adapter (``desired_from_ont_unit``) and
the planner's own emission sites. Each erases the difference between "nobody
configured this" and "an operator chose this", and the reconciler then writes
the placeholder: a blank SSID over the customer's network name, an empty PSK
over their pre-shared key, profile-id 0 into a live ONT authorization.

These tests pin the guards, and walk the AST of all three layers so a
newly-added default cannot enter the code without being registered and
adjudicated.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.services.control_plane_intent import DesiredValueAuthority
from app.services.network import effective_ont_config as composer
from app.services.network.reconcile import (
    AcsSetWifiConfig,
    OltAuthorize,
    OltModifyLineProfile,
    OltModifyServiceProfile,
    compute_plan,
)
from app.services.network.reconcile import adapters as reconcile_adapters
from app.services.network.reconcile import planner as reconcile_planner
from app.services.network.reconcile.sentinels import (
    RULES,
    is_deliverable,
    is_unset,
    rules_by_authority,
    rules_by_layer,
)
from tests.test_reconcile_planner import (
    _desired,
    _informed_acs_observed,
    _observed,
    _olt_observed,
)

# ── Three-layer audit ───────────────────────────────────────────────────────


def _values_get_key(node: ast.AST) -> str | None:
    """The literal key of a ``values.get("k")`` call, if that is what it is."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != "values":
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    key = node.args[0].value
    return key if isinstance(key, str) else None


def _tree(func) -> ast.AST:
    return ast.parse(inspect.getsource(func))


def _composer_defaults() -> set[str]:
    """Dotted config paths that ``cfg(..., default=<literal>)`` fills in.

    A default of ``None`` is not a sentinel — it preserves "unknown" — so it is
    correctly excluded.
    """
    found: set[str] = set()
    for node in ast.walk(_tree(composer._values_from_assignment)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "cfg":
            continue
        default = next(
            (kw.value for kw in node.keywords if kw.arg == "default"),
            None,
        )
        if not isinstance(default, ast.Constant) or default.value is None:
            continue
        path = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if path:
            found.add(".".join(str(part) for part in path))
    return found


def _adapter_coercions() -> set[str]:
    """``values.get("k") or <literal>`` and ``_bool_or_default(values.get("k"))``."""
    found: set[str] = set()
    for node in ast.walk(_tree(reconcile_adapters.desired_from_ont_unit)):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            key = _values_get_key(node.values[0])
            if key is not None:
                found.add(key)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_bool_or_default" and node.args:
                key = _values_get_key(node.args[0])
                if key is not None:
                    found.add(key)
    return found


def _planner_emission_defaults() -> set[str]:
    """``desired.<attr> or <literal>`` at an action-construction site.

    Takes the last ``desired.<attr>`` before the trailing constant, so chained
    fallbacks (``observed... or desired... or "InternetGatewayDevice"``) are
    attributed to the desired-state field they stand in for. A trailing ``None``
    is skipped: normalising an empty value back to unknown is the correct
    behaviour, not a sentinel.
    """
    found: set[str] = set()
    for node in ast.walk(_tree(reconcile_planner)):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        last = node.values[-1]
        if not isinstance(last, ast.Constant) or last.value is None:
            continue
        attrs = [
            inner.attr
            for operand in node.values[:-1]
            for inner in ast.walk(operand)
            if isinstance(inner, ast.Attribute)
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "desired"
        ]
        if attrs:
            found.add(attrs[-1])
    return found


def _registered_identifiers() -> set[str]:
    """Every identifier the registry knows, across all three naming schemes.

    A default is often applied at more than one layer — ``wan.ip_protocol`` is
    filled by the composer and the adapter re-applies ``or "ipv4"`` over the
    result. The registry carries one rule per field, whose ``layer`` names
    where the substitution *dominates*, because that is what determines how the
    detector must measure it. The audit therefore asks the weaker question
    "is this default known and adjudicated?" at every site.
    """
    identifiers: set[str] = set()
    for rule in RULES:
        identifiers.update(
            value for value in (rule.config_path, rule.source_key, rule.field) if value
        )
    return identifiers


@pytest.mark.parametrize(
    ("layer", "discover"),
    [
        ("composer", _composer_defaults),
        ("adapter", _adapter_coercions),
        ("planner", _planner_emission_defaults),
    ],
)
def test_every_default_on_the_desired_state_path_is_registered(layer, discover):
    """A default at any layer must be registered before it can ship.

    Auditing one layer is worse than useless: the composer's ``ip_protocol``
    default makes the adapter's coercion unreachable, so an adapter-only audit
    would report zero affected devices for a rule that fires on every
    unconfigured ONT.
    """
    unregistered = discover() - _registered_identifiers()
    assert not unregistered, (
        f"unregistered {layer}-layer defaults: {sorted(unregistered)}. "
        "Add each to reconcile.sentinels with a disposition so the "
        "blast-radius detector counts it before the sweep is re-enabled."
    )


def test_layer_annotation_matches_where_the_default_dominates():
    """A composer-dominated rule must measure against config paths.

    Registering it as adapter-layer would make the detector read ``values``,
    where the composer default is already applied — the fake zero.
    """
    for rule in rules_by_layer("composer"):
        assert rule.config_path, rule.field
        assert rule.config_path in _composer_defaults(), rule.field


def test_registry_fields_exist_on_desired_state():
    desired = _desired()
    for rule in RULES:
        assert hasattr(desired, rule.field), rule.field


def test_registry_has_no_duplicate_fields():
    fields = [rule.field for rule in RULES]
    assert len(fields) == len(set(fields))


def test_inadmissible_entries_are_the_customer_visible_ones():
    refused = {
        rule.field for rule in rules_by_authority(DesiredValueAuthority.inadmissible)
    }
    assert refused == {
        "wifi_ssid",
        "wifi_password_ref",
        "line_profile_id",
        "service_profile_id",
    }


def test_review_status_alone_never_makes_a_default_executable():
    """The split blocker 2 asked for: authority is not adjudication.

    An entry may not claim ``declared_default`` while its review is undecided;
    the owner's declaration contract raises on construction, so a table that
    tried it would fail at import rather than in the field.
    """
    for rule in RULES:
        declaration = rule.declaration  # constructing validates the claim
        if declaration.authority is DesiredValueAuthority.declared_default:
            assert declaration.adjudication.value == "approved", rule.field
            assert declaration.declared_by, rule.field


def test_delegated_entries_name_their_refusing_owner():
    delegated = list(rules_by_authority(DesiredValueAuthority.delegated))
    assert {rule.field for rule in delegated} == {
        "wan_pppoe_username",
        "wan_pppoe_password_ref",
    }
    for rule in delegated:
        assert rule.declared_by == "network.ppp_delivery_authorization"
        assert rule.adjudication.value == "refused"


def test_delegated_values_are_not_guarded_a_second_time():
    """Blocker 3: refused by the PPP owner, not by a competing inline guard."""
    assert is_deliverable("wan_pppoe_username", "")
    assert is_deliverable("wan_pppoe_password_ref", "")


# ── Sentinel predicates ─────────────────────────────────────────────────────


def test_is_unset_matches_only_the_registered_sentinel():
    assert is_unset("wifi_ssid", "")
    assert not is_unset("wifi_ssid", "KURSI")
    assert is_unset("line_profile_id", 0)
    assert not is_unset("line_profile_id", 40)


def test_is_unset_does_not_conflate_false_with_zero():
    """``False == 0`` in Python; a boolean must not trip an integer sentinel."""
    assert not is_unset("line_profile_id", False)
    assert not is_unset("service_profile_id", False)


def test_authority_debt_entries_still_execute_and_are_recorded_as_such():
    """Listed as debt, not as permission — but honest that they execute."""
    assert is_deliverable("dhcp_enabled", True)
    assert is_deliverable("dhcp_pool_min", "192.168.100.2")
    assert is_deliverable("cr_username", "admin")


def test_unregistered_field_is_always_deliverable():
    assert is_deliverable("mgmt_ip", "")


def test_fires_for_distinguishes_falsy_from_absent():
    falsy = next(rule for rule in RULES if rule.field == "wifi_ssid")
    absent = next(rule for rule in RULES if rule.field == "dhcp_enabled")
    keys = frozenset()

    assert falsy.fires_for({}, keys) is True
    assert falsy.fires_for({"wifi_ssid": ""}, keys) is True
    assert falsy.fires_for({"wifi_ssid": "KURSI"}, keys) is False

    assert absent.fires_for({}, keys) is True
    assert absent.fires_for({"lan_dhcp_enabled": False}, keys) is False
    assert absent.fires_for({"lan_dhcp_enabled": True}, keys) is False


def test_composer_rule_is_measured_against_config_keys_not_values():
    """The whole point of the layer distinction.

    ``values["ip_protocol"]`` is always populated because the composer already
    defaulted it, so measuring there would report zero affected devices for a
    rule that fires on every unconfigured ONT.
    """
    rule = next(rule for rule in RULES if rule.field == "ipv6_enabled")
    values = {"ip_protocol": "ipv4"}

    assert rule.fires_for(values, frozenset()) is True
    assert rule.fires_for(values, frozenset({"wan.ip_protocol"})) is False


def test_unmeasurable_rule_refuses_to_report_a_count():
    """A fake zero is the failure mode this exercise exists to prevent."""
    rule = next(rule for rule in RULES if not rule.measurable)
    with pytest.raises(ValueError, match="not measurable"):
        rule.fires_for({}, frozenset())


# ── Planner guards: WiFi ────────────────────────────────────────────────────


def _wifi_action(plan) -> AcsSetWifiConfig | None:
    for action in plan.actions:
        if isinstance(action, AcsSetWifiConfig):
            return action
    return None


def _present_olt(**overrides):
    defaults = dict(
        olt_present=True,
        olt_match_state="match",
        olt_run_state="online",
        olt_line_profile_id=40,
        olt_service_profile_id=42,
    )
    defaults.update(overrides)
    return _olt_observed(**defaults)


def test_sweep_does_not_blank_an_unset_ssid():
    """The 2026-07-14 P0: the sweep writes "" over a live customer SSID."""
    plan = compute_plan(
        _desired(wifi_ssid=""),
        _observed(
            olt=_present_olt(), acs=_informed_acs_observed(acs_observed_ssid="KURSI")
        ),
        "sweep",
    )
    assert _wifi_action(plan) is None


def test_sweep_still_repairs_a_real_ssid_drift():
    plan = compute_plan(
        _desired(wifi_ssid="KURSI-NEW"),
        _observed(
            olt=_present_olt(), acs=_informed_acs_observed(acs_observed_ssid="KURSI")
        ),
        "sweep",
    )
    action = _wifi_action(plan)
    assert action is not None
    assert action.ssid == "KURSI-NEW"


def test_fresh_bring_up_does_not_push_an_unset_ssid():
    """Bring-up pushes WiFi regardless of drift — the guard must hold there."""
    plan = compute_plan(
        _desired(wifi_ssid=""),
        _observed(acs=_informed_acs_observed()),
        "sync",
    )
    action = _wifi_action(plan)
    assert action is None or action.ssid is None


def test_bootstrap_does_not_push_an_unset_psk():
    plan = compute_plan(
        _desired(wifi_password_ref=""),
        _observed(acs=_informed_acs_observed()),
        "bootstrap",
    )
    action = _wifi_action(plan)
    assert action is None or action.password_ref is None


def test_bootstrap_still_pushes_a_real_psk():
    plan = compute_plan(
        _desired(),
        _observed(acs=_informed_acs_observed()),
        "bootstrap",
    )
    action = _wifi_action(plan)
    assert action is not None
    assert action.password_ref == "bao://wifi"


def test_fresh_sync_does_not_push_an_unset_psk():
    plan = compute_plan(
        _desired(wifi_password_ref=""),
        _observed(acs=_informed_acs_observed()),
        "sync",
    )
    action = _wifi_action(plan)
    assert action is None or action.password_ref is None


# ── Planner guards: OLT profiles ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "action_type"),
    [
        ("line_profile_id", OltModifyLineProfile),
        ("service_profile_id", OltModifyServiceProfile),
    ],
)
def test_unset_profile_id_emits_no_olt_modify(field, action_type):
    """Profile-id 0 is a silent no-op on Huawei — writing it fakes convergence."""
    plan = compute_plan(
        _desired(**{field: 0}),
        _observed(olt=_present_olt()),
        "sweep",
    )
    assert not [a for a in plan.actions if isinstance(a, action_type)]


@pytest.mark.parametrize(
    ("field", "action_type", "attr"),
    [
        ("line_profile_id", OltModifyLineProfile, "line_profile_id"),
        ("service_profile_id", OltModifyServiceProfile, "service_profile_id"),
    ],
)
def test_real_profile_drift_still_repairs(field, action_type, attr):
    plan = compute_plan(
        _desired(**{field: 99}),
        _observed(olt=_present_olt()),
        "sweep",
    )
    emitted = [a for a in plan.actions if isinstance(a, action_type)]
    assert emitted
    assert getattr(emitted[0], attr) == 99


# ── Planner guards: fresh authorization ─────────────────────────────────────
#
# ``ont add`` carries both profile bindings. Unlike the modify path this is not
# a silent no-op: it creates a live ONT authorization with no usable profile.


@pytest.mark.parametrize("field", ["line_profile_id", "service_profile_id"])
def test_absent_ont_is_not_authorized_with_an_unset_profile(field):
    plan = compute_plan(
        _desired(**{field: 0}),
        _observed(olt=_olt_observed(olt_present=False)),
        "sync",
    )
    assert not [a for a in plan.actions if isinstance(a, OltAuthorize)]


@pytest.mark.parametrize("field", ["line_profile_id", "service_profile_id"])
def test_blocked_authorization_is_reported_unrepairable(field):
    """The ONT genuinely cannot converge — reporting it as pending would lie."""
    plan = compute_plan(
        _desired(**{field: 0}),
        _observed(olt=_olt_observed(olt_present=False)),
        "sync",
    )
    presence = [d for d in plan.drifts if d.field == "olt_present"]
    assert presence
    assert presence[0].repairable is False


def test_absent_ont_with_real_profiles_still_authorizes():
    plan = compute_plan(
        _desired(),
        _observed(olt=_olt_observed(olt_present=False)),
        "sync",
    )
    authorize = [a for a in plan.actions if isinstance(a, OltAuthorize)]
    assert authorize
    assert authorize[0].line_profile_id == 40
    assert authorize[0].service_profile_id == 42
    presence = [d for d in plan.drifts if d.field == "olt_present"]
    assert presence[0].repairable is True
