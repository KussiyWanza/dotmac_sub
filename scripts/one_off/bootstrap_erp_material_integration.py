"""Idempotently install and validate the ERP material integration.

Secrets are referenced, never stored in this script. Example:

    ERP_SUB_SERVICE_TOKEN=... ERP_SUB_WEBHOOK_SECRET=... \
      python scripts/one_off/bootstrap_erp_material_integration.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONNECTOR_KEY = "dotmac.erp"
CAPABILITIES = (
    "erp.inventory.read.v1",
    "erp.outbox.deliver.v1",
    "erp.status.read.v1",
    "erp.material_status.webhook.v1",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Persist and validate setup"
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Create disabled bindings and print the callback before secrets exist",
    )
    parser.add_argument("--base-url", default="https://erp.dotmac.io")
    parser.add_argument("--service-token-ref", default="env://ERP_SUB_SERVICE_TOKEN")
    parser.add_argument("--webhook-secret-ref", default="env://ERP_SUB_WEBHOOK_SECRET")
    parser.add_argument("--skip-initial-import", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.apply and args.prepare:
        raise SystemExit("Choose either --prepare or --apply")
    if not args.apply and not args.prepare:
        print("DRY RUN: would install dotmac.erp 1.2.0, bind:")
        for capability in CAPABILITIES:
            print(f"  - {capability}")
        print(f"ERP base URL: {args.base_url}")
        print("Run again with --apply after deployment secrets are present.")
        return 0
    if args.apply:
        for variable in ("ERP_SUB_SERVICE_TOKEN", "ERP_SUB_WEBHOOK_SECRET"):
            if not os.getenv(variable):
                raise SystemExit(f"Required deployment secret is missing: {variable}")
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.integration_platform import IntegrationInstallation
    from app.services.field.material_catalog_sync import run_erp_material_catalog_sync
    from app.services.integrations import installations
    from app.services.integrations.runtime_execution import (
        build_execution_context,
        validate_connection,
    )

    actor = "one-off:bootstrap-erp-material-integration"
    with SessionLocal() as db:
        installation = db.scalar(
            select(IntegrationInstallation).where(
                IntegrationInstallation.connector_key == CONNECTOR_KEY,
                IntegrationInstallation.state != "retired",
            )
        )
        if installation is None:
            installation = installations.create_draft(
                db,
                connector_key=CONNECTOR_KEY,
                name="DotMac ERP material integration",
                environment="production",
                actor=actor,
            )
        installations.create_config_revision(
            db,
            installation_id=installation.id,
            config={
                "base_url": args.base_url,
                "timeout_seconds": 30,
                "max_retries": 3,
                "interactive_timeout_seconds": 5,
                "interactive_max_retries": 1,
            },
            secret_refs={
                "service_credentials": args.service_token_ref,
                "webhook_signing_secret": args.webhook_secret_ref,
            },
            actor=actor,
        )
        bindings = [
            installations.bind_capability(
                db,
                installation_id=installation.id,
                capability_id=capability,
                actor=actor,
            )
            for capability in CAPABILITIES
        ]
        static = installations.validate_static(
            db, installation_id=installation.id, actor=actor
        )
        if not static.valid:
            raise SystemExit(f"Static validation failed: {static.error_codes}")
        if args.apply:
            runtime = build_execution_context(
                db, capability_binding_id=bindings[0].id, allow_disabled=True
            )
            connection = validate_connection(runtime)
            if not connection.valid:
                raise SystemExit(
                    f"ERP connection validation failed: {connection.error_codes}"
                )
            installations.enable_after_connection_validation(
                db,
                installation_id=installation.id,
                connection_result=connection,
                actor=actor,
            )
        db.commit()
        state = "Enabled" if args.apply else "Prepared disabled"
        print(f"{state} installation {installation.id}")
        for binding in bindings:
            print(f"{binding.capability_id}: {binding.id}")
        webhook_binding = next(
            row
            for row in bindings
            if row.capability_id == "erp.material_status.webhook.v1"
        )
        print(
            "ERP callback: "
            f"https://selfcare.dotmac.io/webhooks/erp-material/{webhook_binding.id}"
        )
    if args.apply and not args.skip_initial_import:
        print(f"Initial import: {run_erp_material_catalog_sync()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
