"""Operator adapter for the governed Selfcare purchase-order cutover."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.procurement_purchase_order_cutover import (
    PurchaseOrderBackfillTarget,
    PurchaseOrderCutoverCommand,
    SupplierVerificationMethod,
    VerifiedErpSupplierBinding,
    cut_over_purchase_order_origination,
)


def _target(value: str) -> PurchaseOrderBackfillTarget:
    try:
        project, quote, vendor = value.split(":", 2)
        return PurchaseOrderBackfillTarget(
            installation_project_id=UUID(project),
            approved_quote_id=UUID(quote),
            vendor_id=UUID(vendor),
        )
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "target must be INSTALLATION_UUID:QUOTE_UUID:VENDOR_UUID"
        ) from exc


def _verification(value: str) -> VerifiedErpSupplierBinding:
    try:
        vendor, current_hash, erp_reference, verified_at, method = value.split("|", 4)
        return VerifiedErpSupplierBinding(
            vendor_id=UUID(vendor),
            current_reference_sha256=current_hash,
            erp_supplier_reference=erp_reference,
            verified_at=datetime.fromisoformat(verified_at.replace("Z", "+00:00")),
            method=SupplierVerificationMethod(method),
        )
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "verification must be "
            "VENDOR_UUID|CURRENT_SHA256|ERP_REFERENCE|VERIFIED_AT|METHOD"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically assign purchase-order and purchase-invoice ownership to "
            "Selfcare and stage an explicitly ERP-reconciled historical PO batch."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--command-id", type=UUID, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--target", action="append", type=_target, required=True)
    parser.add_argument(
        "--verification", action="append", type=_verification, required=True
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "applied": False,
                    "error": "Refusing to mutate without --apply.",
                    "target_count": len(args.target),
                }
            )
        )
        return 2

    context = CommandContext.system(
        actor=args.actor,
        scope=args.scope,
        reason=args.reason,
        command_id=args.command_id,
        correlation_id=args.command_id,
        idempotency_key=f"procurement-po-cutover:{args.command_id}",
    )
    command = PurchaseOrderCutoverCommand(
        context=context,
        targets=tuple(args.target),
        supplier_verifications=tuple(args.verification),
    )
    try:
        with db_session_adapter.owner_command_session() as db:
            outcome = cut_over_purchase_order_origination(db, command=command)
    except DomainError as exc:
        print(
            json.dumps(
                {
                    "applied": False,
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "applied": True,
                "command_id": str(outcome.command_id),
                "outbox_event_ids": [str(value) for value in outcome.outbox_event_ids],
                "owner": outcome.owner.value,
                "owned_flows": [value.value for value in outcome.owned_flows],
                "replayed": outcome.replayed,
                "target_count": outcome.target_count,
                "vendor_binding_count": outcome.vendor_binding_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
