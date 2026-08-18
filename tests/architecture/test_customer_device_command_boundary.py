from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_web_and_api_delegate_customer_device_scope_to_owner() -> None:
    owner = (ROOT / "app/services/customer_device_commands.py").read_text()
    web = (ROOT / "app/web/customer/routes.py").read_text()
    api = (ROOT / "app/api/me.py").read_text()

    assert "OntAssignment.subscription_id == subscription.id" in owner
    assert "OntAssignment.subscriber_id == subscriber_id" in owner
    assert "OntAssignment.active.is_(True)" in owner
    assert "reboot_subscription_device(" in web
    assert "update_subscription_wifi(" in web
    assert "reboot_subscription_device(" in api
    assert "update_subscription_wifi(" in api
    assert "configure_customer_wifi(" in owner
    assert "OntFeatureService.set_wifi_config(" not in owner
    assert "customer:device:wifi" in web
    assert "customer:device:wifi" in api


def test_mobile_consumes_canonical_device_command_outcome() -> None:
    repository = (
        ROOT / "mobile/lib/src/repositories/catalog_repository.dart"
    ).read_text()
    screen = (
        ROOT / "mobile/lib/src/features/service/service_detail_screen.dart"
    ).read_text()
    model = (ROOT / "mobile/lib/src/models/device_command.dart").read_text()

    assert "/device/reboot" in repository
    assert "/device/wifi" in repository
    assert "DeviceCommandOutcome.fromJson" in repository
    assert "outcome.message" in screen
    assert "operation_id" in model
