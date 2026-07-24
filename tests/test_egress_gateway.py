"""The egress gateway's allowlist and privilege posture.

Phase 4b of ADR 0005. Naming, allowlist rendering, and the proxy argv are pure,
so the security-relevant decisions are asserted here without invoking Podman.
The live enforcement (allowed passes, denied blocked, direct bypass blocked) is
covered by the Podman-gated integration test.
"""

from __future__ import annotations

import pytest

from app.services.integrations.egress_gateway import (
    EgressAttachment,
    EgressGatewayError,
    PodmanEgressGateway,
    build_proxy_argv,
    network_name,
    proxy_name,
    render_allowlist,
)
from app.services.integrations.egress_policy import EgressPolicy

POLICY = EgressPolicy(hosts=("idp.flutterwave.com", "api.flutterwave.com"))


def _argv(**overrides) -> list[str]:
    params = {
        "connector_key": "flutterwave",
        "policy": POLICY,
        "internal_network": "dm-egress-flutterwave",
        "external_network": "podman",
        "external_gateway": "10.88.0.1",
    }
    params.update(overrides)
    return build_proxy_argv(**params)


def test_names_are_derived_per_connector_and_are_podman_safe():
    assert network_name("dotmac.crm") == "dm-egress-dotmac-crm"
    assert proxy_name("dotmac.crm") == "dm-egress-proxy-dotmac-crm"
    assert network_name("Flutterwave") == "dm-egress-flutterwave"


def test_the_allowlist_is_deterministic_so_an_unchanged_policy_looks_unchanged():
    a = render_allowlist(EgressPolicy(hosts=("b.example", "a.example")))
    b = render_allowlist(EgressPolicy(hosts=("a.example", "b.example")))
    assert a == b == "a.example,b.example"


def test_the_proxy_receives_exactly_the_declared_hosts():
    argv = _argv()
    allowed = next(a for a in argv if a.startswith("--env=ALLOWED_HOSTS="))
    assert allowed == "--env=ALLOWED_HOSTS=api.flutterwave.com,idp.flutterwave.com"


def test_the_proxy_is_dual_homed_onto_internal_then_external():
    argv = _argv()
    assert "--network=dm-egress-flutterwave,podman" in argv


def test_net_admin_is_the_only_privilege_the_proxy_gains():
    """It exists solely to repair the proxy's own default route."""
    argv = _argv()
    assert "--cap-drop=ALL" in argv
    assert "--cap-add=NET_ADMIN" in argv
    assert "--security-opt=no-new-privileges" in argv
    added = [a for a in argv if a.startswith("--cap-add=")]
    assert added == ["--cap-add=NET_ADMIN"]


def test_the_proxy_is_itself_confined():
    argv = _argv()
    assert "--read-only" in argv
    assert any(a.startswith("--memory=") for a in argv)
    assert any(a.startswith("--pids-limit=") for a in argv)


def test_the_allowlist_is_labelled_so_a_stale_proxy_can_be_detected():
    argv = _argv()
    label = next(a for a in argv if a.startswith("--label=io.dotmac.egress.allowlist="))
    assert label.endswith("api.flutterwave.com,idp.flutterwave.com")


def test_attach_refuses_a_connector_that_needs_no_network():
    gateway = PodmanEgressGateway()
    with pytest.raises(EgressGatewayError, match="needs no network"):
        gateway.attach(connector_key="example", policy=EgressPolicy.deny_all())


def test_attach_refuses_installation_hosts_it_cannot_resolve_statically():
    """An empty allowlist would be a proxy that permits nothing; refuse instead."""
    gateway = PodmanEgressGateway()
    policy = EgressPolicy(allow_installation_hosts=True)
    with pytest.raises(EgressGatewayError, match="cannot resolve statically"):
        gateway.attach(connector_key="webhook", policy=policy)


def test_the_attachment_exposes_proxy_variables_in_both_cases():
    attachment = EgressAttachment(network="n", proxy_url="http://10.89.0.2:8888")
    env = attachment.proxy_env()
    assert env["http_proxy"] == env["HTTP_PROXY"] == "http://10.89.0.2:8888"
    assert env["https_proxy"] == env["HTTPS_PROXY"] == "http://10.89.0.2:8888"
