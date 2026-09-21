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


def test_wifi_delivery_scope_crosses_owner_to_reconciler_without_secret_values():
    owner_source = _source("app/services/network/ont_service_configuration.py")
    state_source = _source("app/services/network/reconcile/state.py")

    assert "OntWifiDeliveryScope" in state_source
    assert "changed_fields: frozenset[OntWifiDeliveryField]" in state_source
    assert "wifi_scope = _wifi_delivery_scope(revision)" in owner_source
    assert "wifi_delivery_scope=wifi_scope," in owner_source
    scope_slice = owner_source.split("def _wifi_delivery_scope(", 1)[1].split(
        "def _execution_locked(", 1
    )[0]
    assert "wifi.password" in scope_slice
    assert "wifi_password_ref" in scope_slice
    assert "encrypt_credential" not in scope_slice
    assert "decrypt" not in scope_slice


def test_verification_never_sets_sync_status_directly():
    """``out_of_sync`` has one canonical writer:
    ``app.services.network.ont_status.set_sync_status``, called from inside
    ``reconcile_ont``. The readback-verification path must call INTO
    ``reconcile_ont`` for that decision rather than assign
    ``OntUnit.sync_status``/``out_of_sync`` itself.
    """
    owner_source = _source("app/services/network/ont_service_configuration.py")
    verify_slice = owner_source.split("def _verify_locked(", 1)[1].split(
        "def verify_ont_service_configuration_readback(", 1
    )[0]

    assert "sync_status" not in verify_slice
    assert "out_of_sync" not in verify_slice
    assert "set_sync_status" not in verify_slice


def test_verification_dispatch_is_structurally_readback_only():
    """The dispatched ``ont_service_config_verify.v1`` invocation carries no
    field that could select a write path, and the worker task fixes
    ``force_readback_only=True`` unconditionally — not from any argument.
    """
    dispatch_source = _source("app/services/network_operation_dispatch.py")
    task_source = _source("app/tasks/ont_service_configuration.py")

    verify_invocation_slice = dispatch_source.split(
        "def _ont_service_config_verify_invocation(", 1
    )[1].split("def _cpe_tr069_invocation(", 1)[0]
    assert "verification_attempt" not in verify_invocation_slice
    assert "explicit_repair" not in verify_invocation_slice

    verify_task_slice = task_source.split("def verify_readback(", 1)[1]
    # The task's own signature carries no ``force_readback_only`` parameter
    # (checked against the args-block above the docstring) — every mention
    # of it below is a hardcoded ``=True``, never something a caller,
    # the dispatch payload, or the celery signature could override.
    task_signature = task_source.split("def verify_readback(", 1)[1].split(
        ") -> dict[str, Any]:", 1
    )[0]
    assert "force_readback_only" not in task_signature
    assert "force_readback_only=True" in verify_task_slice
    non_docstring_slice = verify_task_slice.split('"""', 2)[-1]
    assert non_docstring_slice.count("force_readback_only") == 1


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
