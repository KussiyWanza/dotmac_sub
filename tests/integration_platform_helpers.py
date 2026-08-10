from __future__ import annotations

from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult


def enable_capability(
    db,
    *,
    connector_key: str,
    capability_id: str,
    config: dict,
    secret_refs: dict[str, str],
    policy: dict | None = None,
):
    """Create an enabled test installation without resolving secret material."""
    installation = installations.create_draft(
        db,
        connector_key=connector_key,
        name=f"Test {connector_key} {capability_id}",
        environment="test",
    )
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config=config,
        secret_refs=secret_refs,
    )
    binding = installations.bind_capability(
        db,
        installation_id=installation.id,
        capability_id=capability_id,
        policy={"default": True, **(policy or {})},
    )
    installations.validate_static(db, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    return binding


def enable_erp_capability(db, capability_id: str):
    return enable_capability(
        db,
        connector_key="dotmac.erp",
        capability_id=capability_id,
        config={
            "base_url": "https://erp.example.test",
            "timeout_seconds": 5,
            "max_retries": 1,
        },
        secret_refs={
            "service_credentials": "env://ERP_TEST_TOKEN",
            "webhook_signing_secret": "env://ERP_TEST_WEBHOOK_SECRET",
        },
    )


def enable_crm_inbound(db, monkeypatch, *, signing_secret: str):
    monkeypatch.setenv("CRM_TEST_SERVICE_TOKEN", "test-service-token")
    monkeypatch.setenv("CRM_TEST_WEBHOOK_SECRET", signing_secret)
    return enable_capability(
        db,
        connector_key="dotmac.crm",
        capability_id="crm.events.receive.v1",
        config={"base_url": "https://crm.example.test", "timeout_seconds": 5},
        secret_refs={
            "service_credentials": "env://CRM_TEST_SERVICE_TOKEN",
            "webhook_signing_secret": "env://CRM_TEST_WEBHOOK_SECRET",
        },
    )


def enable_payment_provider(
    db,
    provider_type: str,
    *,
    presentment_priority: int = 0,
):
    installation = installations.create_draft(
        db,
        connector_key=provider_type,
        name=f"Test {provider_type}",
        environment="test",
    )
    secret_refs = {
        "gateway_credentials": f"env://{provider_type.upper()}_TEST_SECRET",
        "public_key": f"env://{provider_type.upper()}_TEST_PUBLIC",
    }
    if provider_type == "flutterwave":
        secret_refs["webhook_signing_secret"] = "env://FLUTTERWAVE_TEST_WEBHOOK"
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={
            "base_url": f"https://{provider_type}.example.test",
            "timeout_seconds": 5,
            "default_currency": "NGN",
        },
        secret_refs=secret_refs,
    )
    bindings = {}
    for capability_id in (
        "payments.intent.v1",
        "payments.webhook.v1",
        "payments.reconcile.v1",
        "payments.refund.v1",
    ):
        bindings[capability_id] = installations.bind_capability(
            db,
            installation_id=installation.id,
            capability_id=capability_id,
            policy={
                "default": True,
                **(
                    {"presentment_priority": presentment_priority}
                    if capability_id == "payments.intent.v1"
                    else {}
                ),
            },
        )
    installations.validate_static(db, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    return bindings
