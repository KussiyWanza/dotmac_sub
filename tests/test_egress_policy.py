"""EgressPolicy is a default-deny projection of a connector's manifest.

Phase 4 of ADR 0005. No Podman needed. Pins that an empty allowlist means no
network, and that the transport fails closed — never falls back to open
egress — for a connector that declares hosts before a gateway exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.integrations.egress_gateway import (
    EgressAttachment,
    EgressGatewayError,
)
from app.services.integrations.egress_policy import EgressPolicy
from app.services.integrations.external_runner import RunnerTransportError
from app.services.integrations.manifest import (
    ConnectorManifest,
    ConnectorRuntimeType,
    EgressManifest,
    RuntimeManifest,
)
from app.services.integrations.podman_transport import PodmanTransport
from app.services.integrations.runner_protocol import (
    ConnectorPin,
    RunnerRequest,
    RunnerVerb,
)

IMAGE = "ghcr.io/dotmac/connector-example@sha256:" + "a" * 64
SECRET = "sk_live_must_not_be_written_when_egress_refuses"


def _manifest(egress: EgressManifest) -> ConnectorManifest:
    return ConnectorManifest(
        key="example",
        name="Example",
        version="1.0.0",
        connector_type="payment",
        description="Example.",
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.external_oci,
            image="ghcr.io/dotmac/connector-example",
            digest="sha256:" + "a" * 64,
        ),
        egress=egress,
    )


def test_deny_all_needs_no_network():
    assert EgressPolicy.deny_all().requires_network is False


def test_a_connector_with_no_declared_hosts_needs_no_network():
    policy = EgressPolicy.from_manifest(_manifest(EgressManifest()))
    assert policy.hosts == ()
    assert policy.requires_network is False


def test_declared_hosts_are_carried_sorted_and_require_network():
    policy = EgressPolicy.from_manifest(
        _manifest(EgressManifest(hosts=("idp.flutterwave.com", "api.flutterwave.com")))
    )
    assert policy.hosts == ("api.flutterwave.com", "idp.flutterwave.com")
    assert policy.requires_network is True


def test_allow_installation_hosts_requires_network_even_with_no_static_hosts():
    policy = EgressPolicy.from_manifest(
        _manifest(EgressManifest(allow_installation_hosts=True))
    )
    assert policy.requires_network is True


def test_a_no_egress_transport_uses_network_none():
    transport = PodmanTransport(egress=EgressPolicy.deny_all())
    # Resolving the network is what a run would do first; it must be a hard deny.
    network, proxy_env = transport._resolve_egress("example")
    assert network == "none"
    assert proxy_env == {}


def test_a_transport_with_egress_hosts_but_no_gateway_fails_closed():
    transport = PodmanTransport(egress=EgressPolicy(hosts=("api.paystack.co",)))
    with pytest.raises(RunnerTransportError, match="unrestricted network"):
        transport._resolve_egress("example")


def test_a_gateway_that_cannot_confine_refuses_rather_than_running_open():
    """A broken gateway must never degrade into an unconfined run."""

    class BrokenGateway:
        def attach(self, *, connector_key, policy):
            raise EgressGatewayError("no route to proxy")

    transport = PodmanTransport(
        egress=EgressPolicy(hosts=("api.paystack.co",)),
        egress_gateway=BrokenGateway(),
    )
    with pytest.raises(RunnerTransportError, match="refusing to run unconfined"):
        transport._resolve_egress("example")


def test_an_attached_connector_gets_the_gateway_network_and_proxy_env():
    class StubGateway:
        def attach(self, *, connector_key, policy):
            return EgressAttachment(
                network=f"dm-egress-{connector_key}", proxy_url="http://10.89.0.2:8888"
            )

    transport = PodmanTransport(
        egress=EgressPolicy(hosts=("api.paystack.co",)),
        egress_gateway=StubGateway(),
    )
    network, proxy_env = transport._resolve_egress("paystack")
    assert network == "dm-egress-paystack"
    assert proxy_env["https_proxy"] == "http://10.89.0.2:8888"
    assert proxy_env["HTTPS_PROXY"] == "http://10.89.0.2:8888"


def test_egress_refusal_happens_before_any_secret_is_written(tmp_path):
    """A connector we refuse on egress grounds must not have secrets written."""
    transport = PodmanTransport(
        egress=EgressPolicy(hosts=("api.paystack.co",)),
        runtime_dir=str(tmp_path),
    )
    with pytest.raises(RunnerTransportError, match="unrestricted network"):
        transport.exchange(
            request=_execute_request(),
            image_ref=IMAGE,
            secret_material={"gateway_credentials": SECRET},
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        )
    # No secret env file was left in the runtime dir.
    assert list(tmp_path.glob("dm-runner-secret-*.env")) == []


def _execute_request() -> RunnerRequest:
    from app.services.integrations.runtime import OperationEnvelope, OperationTrigger

    envelope = OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="c",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id="payments.intent.v1",
        connector_key="example",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.interactive,
        idempotency_key="i",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        payload={"action": "x"},
    )
    return RunnerRequest(
        verb=RunnerVerb.execute,
        connector=ConnectorPin(
            key="example", version="1.0.0", manifest_digest="a" * 64
        ),
        config={},
        envelope=envelope,
    )
