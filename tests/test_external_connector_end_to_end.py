"""An external connector executes end to end, through every owner.

Phase 6 of ADR 0005. Everything before this proved a layer in isolation. This
drives the whole path exactly as production would: the installations owner
creates, configures, grants, validates and enables a real ``external_oci``
installation; ``build_execution_context`` resolves it through the manifest tier;
and ``make_operation_executor`` runs a capability that lands in a real,
confined, rootless container and comes back as a typed ``OperationResult``.

The connector is registered only for the duration of the test, so proving the
tier does not put an example connector in the production catalogue.

Skipped where Podman or the example image is unavailable. Meant to run on
seabone.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.services.integrations import installations, registry
from app.services.integrations.manifest import (
    CapabilityManifest,
    CapabilityMode,
    ConnectorManifest,
    ConnectorRuntimeType,
    EgressManifest,
    RuntimeManifest,
)
from app.services.integrations.runtime import (
    OperationStatus,
    OperationTrigger,
    ValidationResult,
)
from app.services.integrations.runtime_execution import (
    build_execution_context,
    make_operation_executor,
)

pytestmark = pytest.mark.skipif(
    shutil.which("podman") is None, reason="rootless Podman not available"
)

CONNECTOR_KEY = "echo.external"
CAPABILITY = "payments.intent.v1"
_IMAGE_TAG = "localhost/dotmac-echo-connector:pytest"


def _repo_digest_ref(tag: str) -> str | None:
    """The digest-addressed reference Podman can actually run."""
    result = subprocess.run(
        ["podman", "image", "inspect", tag, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    ref = result.stdout.strip()
    return ref if result.returncode == 0 and "@sha256:" in ref else None


@pytest.fixture
def external_echo_connector(monkeypatch):
    """Register the example connector as external_oci for this test only."""
    ref = _repo_digest_ref(_IMAGE_TAG)
    if ref is None:
        pytest.skip(f"{_IMAGE_TAG} not built with a digest on this host")
    image, _, digest = ref.partition("@")

    manifest = ConnectorManifest(
        key=CONNECTOR_KEY,
        name="Echo External",
        version="1.0.0",
        connector_type="example",
        description="Example out-of-process connector.",
        catalogue_visible=False,
        runtime=RuntimeManifest(
            type=ConnectorRuntimeType.external_oci, image=image, digest=digest
        ),
        capabilities=(
            CapabilityManifest(id=CAPABILITY, modes=(CapabilityMode.interactive,)),
        ),
        # No declared hosts: the connector must run with no network at all.
        egress=EgressManifest(),
    )

    real_require = registry.require_connector_definition

    def fake_require(key: str) -> ConnectorManifest:
        if key == CONNECTOR_KEY:
            return manifest
        return real_require(key)

    for module in ("registry", "installations", "runtime_execution"):
        target = f"app.services.integrations.{module}.require_connector_definition"
        try:
            monkeypatch.setattr(target, fake_require)
        except AttributeError:
            continue
    return manifest


def _enabled_binding(db_session, manifest):
    """Create, configure, grant and enable through the installations owner."""
    installation = installations.create_draft(
        db_session,
        connector_key=CONNECTOR_KEY,
        name="Echo External Test",
        environment="test",
    )
    installations.create_config_revision(
        db_session, installation_id=installation.id, config={}, secret_refs={}
    )
    binding = installations.bind_capability(
        db_session, installation_id=installation.id, capability_id=CAPABILITY
    )
    static = installations.validate_static(db_session, installation_id=installation.id)
    assert static.valid, static.error_codes
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.flush()
    return binding


def test_a_capability_executes_in_a_real_confined_container(
    db_session, external_echo_connector
):
    binding = _enabled_binding(db_session, external_echo_connector)

    context = build_execution_context(db_session, capability_binding_id=binding.id)
    # Resolution went through the manifest tier to the out-of-process runner.
    assert type(context.runner).__name__ == "ExternalOciRunner"

    execute = make_operation_executor(
        context,
        correlation_id="phase6-e2e",
        trigger=OperationTrigger.interactive,
        actor="test",
    )
    result = execute("echo_me", {"amount": 1250})

    assert result.status is OperationStatus.succeeded, result.error_code
    # The payload made it into the container and back.
    assert result.output["echo"]["action"] == "echo_me"
    assert result.output["echo"]["params"] == {"amount": 1250}


def test_the_container_had_no_network_because_it_declared_no_egress(
    db_session, external_echo_connector
):
    """Confinement is derived from the manifest, end to end."""
    binding = _enabled_binding(db_session, external_echo_connector)
    context = build_execution_context(db_session, capability_binding_id=binding.id)
    execute = make_operation_executor(
        context,
        correlation_id="phase6-egress",
        trigger=OperationTrigger.interactive,
        actor="test",
    )
    result = execute("probe_network", {})

    assert result.status is OperationStatus.succeeded, result.error_code
    assert result.output["network_reachable"] is False


def test_a_disabled_installation_is_refused_before_any_container_runs(
    db_session, external_echo_connector
):
    binding = _enabled_binding(db_session, external_echo_connector)
    installations.disable_installation(
        db_session, installation_id=binding.installation_id, reason="test"
    )
    db_session.flush()

    from app.services.integrations.runtime_execution import RuntimeExecutionError

    with pytest.raises(RuntimeExecutionError, match="not enabled"):
        build_execution_context(db_session, capability_binding_id=binding.id)
