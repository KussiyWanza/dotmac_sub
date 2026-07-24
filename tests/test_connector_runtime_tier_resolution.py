"""Runner selection is driven by the manifest's declared runtime tier.

Phase 1 of ADR 0005. Before this, resolution used the bare connector key, so a
connector could inherit whatever executor happened to be registered under its
name regardless of the trust tier it declared. These tests pin the tier as the
authority and pin failing closed when a tier has no executor.
"""

from __future__ import annotations

import pytest

from app.services.integrations.manifest import (
    CapabilityManifest,
    ConnectorManifest,
    ConnectorRuntimeType,
    RuntimeManifest,
)
from app.services.integrations.runtime import RunnerRegistry
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    RuntimeTierUnavailableError,
    external_runner_unavailable,
    resolve_runner,
)

IMAGE = "ghcr.io/dotmac/connector-example"
DIGEST = "sha256:" + "a" * 64


class StubRunner:
    """Stands in for any ConnectorRunner; identity is all these tests need."""

    def __init__(self, label: str) -> None:
        self.label = label


def _manifest(
    runtime: RuntimeManifest,
    *,
    key: str = "example",
    capabilities: tuple[CapabilityManifest, ...] = (),
) -> ConnectorManifest:
    return ConnectorManifest(
        key=key,
        name="Example",
        version="1.0.0",
        connector_type="payment",
        description="Example connector.",
        runtime=runtime,
        capabilities=capabilities,
    )


BUILTIN = _manifest(
    RuntimeManifest(
        type=ConnectorRuntimeType.builtin_worker,
        module="app.services.integrations.connectors.payment_gateway",
    )
)
LEGACY = _manifest(
    RuntimeManifest(
        type=ConnectorRuntimeType.legacy_adapter,
        module="app.services.integrations.connectors.payment_gateway",
    )
)
EXTERNAL = _manifest(
    RuntimeManifest(type=ConnectorRuntimeType.external_oci, image=IMAGE, digest=DIGEST)
)
CATALOGUE = _manifest(RuntimeManifest(type=ConnectorRuntimeType.catalogue_only))


def _registry(label: str = "builtin", key: str = "example") -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register(key, StubRunner(label))
    return registry


def test_builtin_worker_resolves_from_the_in_process_registry():
    runner = resolve_runner(BUILTIN, registry=_registry())
    assert isinstance(runner, StubRunner)
    assert runner.label == "builtin"


def test_legacy_adapter_resolves_from_the_in_process_registry():
    runner = resolve_runner(LEGACY, registry=_registry("legacy"))
    assert runner.label == "legacy"


def test_external_oci_never_inherits_a_runner_registered_under_its_key():
    """The isolation guarantee this phase exists to establish.

    A registry entry sharing the connector key must not satisfy an external
    connector: running isolated code in-process would defeat the tier.
    """
    registry = _registry("in-process runner for 'example'")
    runner = resolve_runner(EXTERNAL, registry=registry)
    assert not isinstance(runner, StubRunner)
    assert type(runner).__name__ == "ExternalOciRunner"


def test_external_oci_resolves_to_an_out_of_process_runner_by_default():
    """Phase 6 made the tier executable; it no longer refuses by default.

    A missing container runtime now surfaces at execution as a transport error
    rather than as an unresolvable tier.
    """
    runner = resolve_runner(EXTERNAL, registry=_registry())
    assert type(runner).__name__ == "ExternalOciRunner"


def test_a_deployment_can_still_disable_the_external_tier_explicitly():
    with pytest.raises(RuntimeTierUnavailableError) as excinfo:
        resolve_runner(
            EXTERNAL,
            registry=_registry(),
            external_factory=external_runner_unavailable,
        )
    assert "no executor in this deployment" in str(excinfo.value)


def test_external_oci_uses_an_injected_factory_when_one_is_supplied():
    """Phase 3 plugs the real OCI supervisor in here without touching callers."""
    seen: list[ConnectorManifest] = []

    def factory(manifest: ConnectorManifest) -> StubRunner:
        seen.append(manifest)
        return StubRunner("oci")

    runner = resolve_runner(EXTERNAL, registry=_registry(), external_factory=factory)
    assert runner.label == "oci"
    assert seen == [EXTERNAL]
    assert seen[0].runtime.image == IMAGE
    assert seen[0].runtime.digest == DIGEST


def test_catalogue_only_is_refused_at_execution_not_just_at_install():
    with pytest.raises(RuntimeExecutionError) as excinfo:
        resolve_runner(CATALOGUE, registry=_registry())
    assert "catalogue-only" in str(excinfo.value)


def test_catalogue_only_refusal_is_not_a_tier_unavailable_error():
    """Distinct failures: a missing executor is not the same as naming no code."""
    with pytest.raises(RuntimeExecutionError) as excinfo:
        resolve_runner(CATALOGUE, registry=_registry())
    assert not isinstance(excinfo.value, RuntimeTierUnavailableError)


def test_unregistered_builtin_raises_a_typed_error_not_a_bare_lookup_error():
    with pytest.raises(RuntimeTierUnavailableError) as excinfo:
        resolve_runner(BUILTIN, registry=RunnerRegistry())
    assert "no runner is registered" in str(excinfo.value)


def test_every_declared_runtime_tier_has_defined_resolution_behaviour():
    """A new tier must not reach production with undefined dispatch."""
    registry = _registry()
    for tier in ConnectorRuntimeType:
        if tier is ConnectorRuntimeType.catalogue_only:
            manifest = _manifest(RuntimeManifest(type=tier))
        elif tier is ConnectorRuntimeType.external_oci:
            manifest = _manifest(RuntimeManifest(type=tier, image=IMAGE, digest=DIGEST))
        else:
            manifest = _manifest(
                RuntimeManifest(
                    type=tier,
                    module="app.services.integrations.connectors.payment_gateway",
                )
            )
        try:
            resolve_runner(manifest, registry=registry)
        except RuntimeExecutionError:
            continue  # An explicit, typed refusal is defined behaviour.
