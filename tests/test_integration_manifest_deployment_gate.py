from __future__ import annotations

from uuid import uuid4

from app.models.integration_platform import IntegrationInstallation
from app.services.integrations import installations
from scripts.integrations.verify_manifest_pins import manifest_pin_report
from tests.integration_platform_helpers import enable_payment_provider


def _enabled_paystack(db_session):
    bindings = enable_payment_provider(db_session, "paystack")
    return bindings["payments.intent.v1"].installation


def test_deployment_gate_accepts_current_enabled_pin(db_session):
    _enabled_paystack(db_session)

    report = manifest_pin_report(
        installations.list_manifest_pin_checks(db_session, enabled_only=True)
    )

    assert report["ok"] is True
    assert report["unavailable_count"] == 0
    assert report["supported_historical_count"] == 0


def test_deployment_gate_reports_supported_historical_adoption_debt(db_session):
    installation = _enabled_paystack(db_session)
    installation.connector_version = "1.0.0"
    installation.manifest_digest = (
        "53791d3e2e06fe1ca128a0e3e8ced86549392af7b6131f61bd21044d71aafc6e"
    )
    db_session.commit()

    report = manifest_pin_report(
        installations.list_manifest_pin_checks(db_session, enabled_only=True)
    )

    assert report["ok"] is True
    assert report["unavailable_count"] == 0
    assert report["supported_historical_count"] == 1
    assert report["installations"][0]["pin_state"] == "supported_historical"


def test_deployment_gate_rejects_unavailable_enabled_pin(db_session):
    installation = _enabled_paystack(db_session)
    installation.manifest_digest = "a" * 64
    db_session.commit()

    report = manifest_pin_report(
        installations.list_manifest_pin_checks(db_session, enabled_only=True)
    )

    assert report["ok"] is False
    assert report["unavailable_count"] == 1
    assert report["installations"][0]["pin_state"] == "unavailable"


def test_deployment_gate_accepts_original_meta_social_production_pin() -> None:
    installation = IntegrationInstallation(
        id=uuid4(),
        connector_key="meta.social",
        connector_version="1.0.0",
        manifest_digest=(
            "c2a13ab30bebb90b312ab4bbcf1d2feaa23ea706100b3e5ea35a101ec566cf08"
        ),
        name="Production Meta Social",
        environment="production",
        state="enabled",
    )

    report = manifest_pin_report((installations.manifest_pin_check(installation),))

    assert report["ok"] is True
    assert report["unavailable_count"] == 0
    assert report["supported_historical_count"] == 1
    assert report["installations"][0]["pin_state"] == "supported_historical"
