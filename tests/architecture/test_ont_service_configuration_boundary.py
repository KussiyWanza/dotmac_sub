"""Architecture guards for lifecycle-safe ONT Configure delivery."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_configure_route_is_only_a_typed_transport_adapter():
    source = _source("app/web/admin/network_onts.py")
    configure_slice = source.split("def ont_configure_submit(", 1)[1].split(
        "def ont_configure_retry(", 1
    )[0]

    assert "ConfigureOntServiceCommand(" in configure_slice
    assert "configure_ont_service(" in configure_slice
    assert "update_ont_config(" not in configure_slice
    assert "reconcile_ont(" not in configure_slice
    assert "create_genieacs_client" not in configure_slice
    assert ".delay(" not in configure_slice
    assert "send_task(" not in configure_slice
    assert "pppoe_password: str = Form" not in configure_slice
    assert "pppoe_username: str = Form" not in configure_slice


def test_worker_claims_dispatch_and_never_creates_an_operation():
    source = _source("app/tasks/ont_service_configuration.py")

    assert "managed_network_operation_dispatch" in source
    assert "execute_ont_service_configuration(" in source
    assert "network_operations.start" not in source
    assert "NetworkOperation(" not in source


def test_template_uses_owner_projection_for_retry_and_hides_ppp_secret_inputs():
    source = _source("templates/admin/network/onts/_configure_form.html")

    assert "lifecycle.next_action.value" in source
    assert "Retry current configuration" in source
    assert 'name="pppoe_password"' not in source
    assert 'name="pppoe_username"' not in source
    assert "Historical attempts and legacy evidence" in source
    assert "latest_failure" not in source


def test_legacy_synchronous_configuration_writer_is_retired():
    package_source = _source("app/services/web_network_ont_actions/__init__.py")
    legacy_source = _source("app/services/web_network_ont_actions/db_config.py")

    assert "update_ont_config" not in package_source
    assert "def update_ont_config" not in legacy_source
