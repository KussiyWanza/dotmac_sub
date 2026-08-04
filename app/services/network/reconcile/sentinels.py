"""Which ONT desired-state values mean "unset" rather than "intended".

Three layers turn a missing configuration value into a concrete one, and each
of them erases the difference between "nobody configured this" and "an operator
chose this":

``composer``
    ``resolve_effective_ont_config`` fills absent desired-config paths with a
    default (``cfg("wan", "ip_protocol", default="ipv4")``).
``adapter``
    ``desired_from_ont_unit`` coerces missing effective values so
    ``OntDesiredState`` can keep non-optional fields (``values.get(...) or ""``).
``planner``
    Action construction substitutes at the emission site
    (``desired.cr_username or "admin"``).

By the time the planner diffs desired against observed, a placeholder is
indistinguishable from intent, so it gets written to the customer's device and
reported as a successful convergence. Auditing only one layer is worse than
useless: a composer default makes the adapter's coercion unreachable, so an
adapter-only audit reports zero affected devices for a rule that fires on every
one of them.

This module is the Huawei ONT provider's declaration table. It decides nothing:
``network.control_plane_intent`` owns the rule, and each entry carries a
:class:`~app.services.control_plane_intent.DesiredValueAuthority` naming who
authorises executing the default —

``inadmissible``
    No owner authorises it. The planner declines to emit and the applier
    refuses the whole action before device contact.
``delegated``
    A different named owner already fails closed on this value. Nothing is
    guarded here; a second refusal would only start disagreeing with the first.
``undeclared``
    Executes today with nothing behind it. Every one is listed in
    ``desired_value_authority_debt.txt``, which an architecture guard keeps
    shrink-only, so the debt can be paid down but never grown.
``declared_default``
    A named owner approved it. Executable — and the contract refuses to accept
    that claim unless the review status also says approved.

Review progress lives separately in ``adjudication`` and never grants
execution: an undecided default is not an authorised one.

The guards and ``scripts/network/ont_sentinel_blast_radius.py`` both read this
table, so a guard cannot silently diverge from the count that justified it, and
``tests/test_reconcile_sentinels.py`` walks the AST of all three layers so a
newly-added default cannot escape classification.

Nothing here records drift for a suppressed field. A value whose desired state
is unknown is not in drift — there is no target to converge on — and marking it
would strand thousands of ONTs permanently ``out_of_sync``. Visibility for
unmanaged fields is the detector's job, not the sync status's. The exception is
a refusal that blocks convergence outright (an ONT that cannot be authorized
without profile bindings); that is real, per-ONT, and recorded unrepairable.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.services.control_plane_intent import (
    DesiredScalar,
    DesiredValueAdjudication,
    DesiredValueAuthority,
    DesiredValueDeclaration,
    is_executable_desired_value,
)

#: Where the default is substituted. Provider-local measurement metadata: it
#: says how to count a rule, not who authorises it.
Layer = Literal["composer", "adapter", "planner"]

#: What makes the substitution happen. ``falsy`` is the ``x or default`` idiom,
#: which also swallows a legitimate ``0``/``False``/``""``; ``absent`` is the
#: ``value is None`` / missing-key form, which does not.
Trigger = Literal["falsy", "absent"]

#: Shrink-only list of fields whose defaults still execute with no declaration.
AUTHORITY_DEBT_BASELINE = Path(__file__).with_name("desired_value_authority_debt.txt")

_INADMISSIBLE = DesiredValueAuthority.inadmissible
_UNDECLARED = DesiredValueAuthority.undeclared
_DELEGATED = DesiredValueAuthority.delegated
_REFUSED = DesiredValueAdjudication.refused
_UNDECIDED = DesiredValueAdjudication.undecided


@dataclass(frozen=True, slots=True)
class SentinelRule:
    """One unset-to-default substitution on the ONT desired-state path."""

    #: ``OntDesiredState`` attribute the substitution ultimately produces.
    field: str
    layer: Layer
    #: The value substituted when the source is absent or falsy.
    sentinel: DesiredScalar
    trigger: Trigger
    authority: DesiredValueAuthority
    adjudication: DesiredValueAdjudication
    #: The device write this sentinel would otherwise reach.
    writes: str
    #: What happens on the customer's device if the sentinel is delivered.
    impact: str
    #: Owner behind ``declared_default`` or ``delegated``.
    declared_by: str | None = None
    #: Dotted path in the raw ``OntUnit.desired_config`` blob. Set for composer
    #: rules, where the only way to see "unset" is the absence of the path —
    #: by the time the value reaches ``values`` the default is already applied.
    config_path: str | None = None
    #: Key in ``resolve_effective_ont_config(...)["values"]``. Set for adapter
    #: and planner rules, which read that mapping.
    source_key: str | None = None

    @property
    def declaration(self) -> DesiredValueDeclaration:
        """The owner-typed declaration this entry asserts.

        Constructing it validates the claim, so an entry that says "executable"
        while its review is undecided fails at import rather than in the field.
        """
        return DesiredValueDeclaration(
            field=self.field,
            sentinel=self.sentinel,
            authority=self.authority,
            adjudication=self.adjudication,
            declared_by=self.declared_by,
        )

    @property
    def measurable(self) -> bool:
        """Whether the detector can count this rule from stored config alone.

        ``False`` means the input comes from somewhere the detector does not
        read — an ACS server row, an observed device value — so the rule must
        be reported as *unmeasured* rather than as zero affected devices.
        """
        return self.config_path is not None or self.source_key is not None

    def fires_for(
        self, values: dict[str, DesiredScalar], config_keys: frozenset[str]
    ) -> bool:
        """Whether this substitution happens for one ONT's stored config.

        ``values`` is ``resolve_effective_ont_config(db, ont)["values"]`` and
        ``config_keys`` is its ``desired_config_keys`` — the exact inputs the
        reconciler reads — so the detector counts what would really be built.

        Raises when called on an unmeasurable rule: silently returning ``False``
        is what produces a fake zero.
        """
        if not self.measurable:
            raise ValueError(
                f"rule {self.field!r} is not measurable from stored config"
            )
        if self.config_path is not None:
            # A composer default fires precisely when the operator never set
            # the path. Reading ``values`` here would always see the default.
            return self.config_path not in config_keys
        if self.source_key is None:
            # Unreachable while ``measurable`` requires one of the two inputs.
            # Stated at the point of use so a later change to that property
            # fails loudly here instead of counting the rule as unaffected --
            # a fake zero is the one outcome this module must never produce.
            raise ValueError(
                f"rule {self.field!r} has neither a config path nor a source key"
            )
        value = values.get(self.source_key)
        return value is None if self.trigger == "absent" else not value


RULES: tuple[SentinelRule, ...] = (
    # ── inadmissible: guarded in planner and applier ────────────────────────
    SentinelRule(
        field="wifi_ssid",
        layer="adapter",
        source_key="wifi_ssid",
        sentinel="",
        trigger="falsy",
        authority=_INADMISSIBLE,
        adjudication=_REFUSED,
        writes="AcsSetWifiConfig.ssid",
        impact=(
            "Blanks the customer's SSID. Reached from the automatic sweep on "
            "observed drift and from the fresh bring-up push, which fires "
            "regardless of drift."
        ),
    ),
    SentinelRule(
        field="wifi_password_ref",
        layer="adapter",
        source_key="wifi_password",
        sentinel="",
        trigger="falsy",
        authority=_INADMISSIBLE,
        adjudication=_REFUSED,
        writes="AcsSetWifiConfig.password_ref",
        impact=(
            "Clears the WPA pre-shared key, leaving the WLAN open. Not reached "
            "by the sweep (the PSK is unobservable) but reached on BOOTSTRAP "
            "and on a fresh operator sync."
        ),
    ),
    SentinelRule(
        field="line_profile_id",
        layer="adapter",
        source_key="authorization_line_profile_id",
        sentinel=0,
        trigger="falsy",
        authority=_INADMISSIBLE,
        adjudication=_REFUSED,
        writes="OltAuthorize.line_profile_id, OltModifyLineProfile.line_profile_id",
        impact=(
            "On the modify path profile-id 0 is silently a no-op on Huawei "
            "OLTs, so the action reports success while the OLT is unchanged. "
            "On the authorize path it is worse: ``ont add`` carries both "
            "bindings, so the ONT is authorized live with no usable profile."
        ),
    ),
    SentinelRule(
        field="service_profile_id",
        layer="adapter",
        source_key="authorization_service_profile_id",
        sentinel=0,
        trigger="falsy",
        authority=_INADMISSIBLE,
        adjudication=_REFUSED,
        writes=(
            "OltAuthorize.service_profile_id, "
            "OltModifyServiceProfile.service_profile_id"
        ),
        impact="Same silent no-op and same broken authorization as the line profile.",
    ),
    # ── delegated: a different named owner already fails closed ─────────────
    SentinelRule(
        field="wan_pppoe_username",
        layer="planner",
        source_key="pppoe_username",
        sentinel="",
        trigger="falsy",
        authority=_DELEGATED,
        adjudication=_REFUSED,
        declared_by="network.ppp_delivery_authorization",
        writes="AcsSetPppoe.username, OltOmciPppoe.username",
        impact=(
            "An empty PPPoE username is not a live adjudication — it is already "
            "refused. PPP delivery is authorised per exact active service "
            "intent, an ONT with no resolved credential produces no authorising "
            "ruling, and apply withholds every PPP-purpose action without one. "
            "Recorded against that owner rather than guarded again here: two "
            "owners refusing the same value independently is how refusals start "
            "disagreeing."
        ),
    ),
    SentinelRule(
        field="wan_pppoe_password_ref",
        layer="planner",
        source_key="pppoe_password",
        sentinel="",
        trigger="falsy",
        authority=_DELEGATED,
        adjudication=_REFUSED,
        declared_by="network.ppp_delivery_authorization",
        writes="AcsSetPppoe.password_ref, OltOmciPppoe.password_ref",
        impact="Refused by the same PPP delivery authorization as the username.",
    ),
    # ── undeclared: executes today; on the shrink-only debt baseline ────────
    SentinelRule(
        field="ipv6_enabled",
        layer="composer",
        config_path="wan.ip_protocol",
        # The adapter re-applies ``or "ipv4"`` over the composed value. Both
        # names are recorded; ``config_path`` is what the detector measures,
        # because by the time the value reaches ``values`` it is never absent.
        source_key="ip_protocol",
        sentinel="ipv4",
        trigger="absent",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetIpv6.enabled",
        impact=(
            'resolve_effective_ont_config defaults the path to "ipv4", which '
            "the adapter turns into ipv6_enabled=False, and the fresh bring-up "
            "branch pushes it regardless of drift — so a dual-stack service "
            "whose ip_protocol never reached desired_config has IPv6 torn "
            "down. Because the composer default lands first, this is invisible "
            "to any audit that only reads the adapter."
        ),
    ),
    SentinelRule(
        field="wan_pppoe_instance_index",
        layer="composer",
        config_path="wan.instance_index",
        source_key="wan_instance_index",
        sentinel=1,
        trigger="absent",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetPppoe.instance_index",
        impact=(
            "WANPPPConnection.1 is the real first instance on deployed "
            "firmware, so the substitution is almost certainly right. Almost "
            "certainly right is not declared."
        ),
    ),
    SentinelRule(
        field="dhcp_enabled",
        layer="adapter",
        source_key="lan_dhcp_enabled",
        sentinel=True,
        trigger="absent",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetDhcpServer.enabled",
        impact=(
            "The bring-up branch enables the LAN DHCP server because deployed "
            "firmware ships it off. Documented in feedback_ont_setup_defaults "
            "but never declared by an owner, and it switches a DHCP server on "
            "under bridged and router-behind ONTs too."
        ),
    ),
    SentinelRule(
        field="dhcp_pool_min",
        layer="adapter",
        source_key="lan_dhcp_start",
        sentinel="192.168.100.2",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetDhcpServer.pool_min",
        impact=(
            "The whole DHCP block is pushed when any field differs, so an "
            "unset pool overwrites an operator-configured LAN range with the "
            "module default."
        ),
    ),
    SentinelRule(
        field="dhcp_pool_max",
        layer="adapter",
        source_key="lan_dhcp_end",
        sentinel="192.168.100.254",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetDhcpServer.pool_max",
        impact="Same block write as dhcp_pool_min.",
    ),
    SentinelRule(
        field="tr069_profile_id",
        layer="adapter",
        source_key="tr069_olt_profile_id",
        sentinel=0,
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="OltTr069ServerConfig.profile_id",
        impact=(
            "In practice unreachable: validate_desired refuses a non-positive "
            "tr069_profile_id at the boundary and at the top of reconcile_ont. "
            "Listed rather than declared because that refusal belongs to the "
            "validator, not to a declaration about this default."
        ),
    ),
    SentinelRule(
        field="wan_pppoe_wcd_index",
        layer="adapter",
        source_key="pppoe_wcd_index",
        sentinel=1,
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetPppoe.wcd_index",
        impact=(
            "WANConnectionDevice.1 is the deployed default. If a vendor ever "
            "indexes from 0 this becomes a live defect, which is exactly why "
            "an undeclared default is worth listing."
        ),
    ),
    SentinelRule(
        field="cr_username",
        layer="planner",
        source_key="cr_username",
        sentinel="admin",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetManagementServer.cr_username",
        impact=(
            "Substituted at the emission site. _management_server_differs "
            "compares with a raw != rather than _observed_differs, so an "
            "unread value counts as drift and the write is re-emitted every "
            "pass. CR credentials gate synchronous NBI delivery, so refusing "
            "this needs the owner's decision, not an inline change."
        ),
    ),
    SentinelRule(
        field="cr_password_ref",
        layer="planner",
        source_key="cr_password",
        sentinel="",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetManagementServer.cr_password_ref",
        impact=(
            "An ONT with no recorded CR password has its "
            "ConnectionRequestPassword set empty. Whether that is an intended "
            "known-state reset or a credential wipe is the open question."
        ),
    ),
    SentinelRule(
        field="wan_vlan",
        layer="planner",
        source_key="wan_vlan",
        sentinel=0,
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetPppoe.vlan, AcsSetWanIp.vlan",
        impact=(
            "VLAN 0 is not a valid customer VLAN. validate_desired bounds every "
            "VLAN to [1, 4094], so a stored 0 cannot pass the boundary; the "
            "emission-site default would reintroduce it for a desired state "
            "built elsewhere."
        ),
    ),
    SentinelRule(
        field="wan_gem_index",
        layer="planner",
        source_key="wan_gem_index",
        sentinel=1,
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="OltOmciWanConfig.gem_index",
        impact="First GEM port is the deployed default; plausible, undeclared.",
    ),
    SentinelRule(
        field="mgmt_subnet_mask",
        layer="planner",
        source_key="mgmt_subnet",
        sentinel="255.255.255.0",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="OltIpconfig.subnet_mask",
        impact=(
            "A /24 is assumed when the management pool never supplied a mask. "
            "Wrong on any pool that is not /24, and the management IP-host "
            "write is then silently misconfigured."
        ),
    ),
    SentinelRule(
        field="mgmt_gateway",
        layer="planner",
        source_key="mgmt_gateway",
        sentinel="",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="OltIpconfig.gateway",
        impact=(
            "An empty gateway is written when the pool supplied none, leaving "
            "the ONT management interface without a route off-subnet."
        ),
    ),
    SentinelRule(
        field="acs_username",
        layer="planner",
        source_key=None,
        sentinel="",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetManagementServer.acs_username",
        impact=(
            "Read from the ACS server row rather than desired config, so the "
            "detector cannot count it per-ONT. Reported as unmeasured."
        ),
    ),
    SentinelRule(
        field="tr069_data_model_root",
        layer="planner",
        source_key=None,
        sentinel="InternetGatewayDevice",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetManagementServer.data_model_root, AcsSetWanIp paths",
        impact=(
            "Falls back to the TR-098 root when neither the observed ACS data "
            "model nor the stored root is known. On a TR-181 device every "
            "parameter path built from it is wrong, and the write faults "
            "rather than converging. Depends on an observed value, so it is "
            "reported as unmeasured."
        ),
    ),
    SentinelRule(
        field="acs_device_id",
        layer="planner",
        source_key=None,
        sentinel="",
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="(none — normalised straight back to None)",
        impact=(
            'Not a live substitution: ``(desired.acs_device_id or "").strip() '
            "or None`` turns the empty string back into None and the planner "
            "fails closed rather than fabricating an ACS identifier. Listed so "
            "a reader who finds the idiom sees it was examined, not missed."
        ),
    ),
    SentinelRule(
        field="periodic_inform_interval_sec",
        layer="adapter",
        source_key=None,
        sentinel=300,
        trigger="falsy",
        authority=_UNDECLARED,
        adjudication=_UNDECIDED,
        writes="AcsSetManagementServer.inform_interval_sec",
        impact=(
            "Read from the ACS server row, not desired config, so the detector "
            "cannot count it per-ONT. Listed because an unread observed value "
            "compares unequal to 300 and keeps the ManagementServer write "
            "permanently in drift."
        ),
    ),
)

_BY_FIELD: dict[str, SentinelRule] = {rule.field: rule for rule in RULES}


def rules_by_authority(authority: DesiredValueAuthority) -> Iterator[SentinelRule]:
    """Yield the registered entries carrying ``authority``."""
    return (rule for rule in RULES if rule.authority is authority)


def rules_by_layer(layer: Layer) -> Iterator[SentinelRule]:
    """Yield the registered entries substituted at ``layer``."""
    return (rule for rule in RULES if rule.layer == layer)


def authority_debt_baseline() -> frozenset[str]:
    """Fields on the checked-in shrink-only authority-debt list."""
    return frozenset(
        line.strip()
        for line in AUTHORITY_DEBT_BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def is_deliverable(field: str, value: DesiredScalar) -> bool:
    """Whether ``value`` may be written to a device as ``field``.

    The ONT provider's instance of the control-plane rule: this table supplies
    the sentinel and the authority, and ``network.control_plane_intent``
    decides. ``True`` for every field with no registered entry, so callers
    apply it uniformly without knowing the table's contents.
    """
    rule = _BY_FIELD.get(field)
    if rule is None:
        return True
    return is_executable_desired_value(value, declaration=rule.declaration)


def is_unset(field: str, value: DesiredScalar) -> bool:
    """Whether ``value`` is a sentinel this provider refuses to deliver."""
    return not is_deliverable(field, value)
