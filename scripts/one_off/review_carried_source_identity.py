#!/usr/bin/env python3
"""Preview or record one reviewed pre-handoff Sub-native billing decision.

Dry-run is the default and the report is PII-free. Apply mode records only the
dual-reviewed provenance decision: it never assigns a Splynx identifier, writes
money, or materializes an opening balance.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from app.db import SessionLocal
from app.services.carried_source_identity_adjudication import (
    OWNER,
    ConfirmCarriedSourceIdentityCommand,
    confirm_carried_source_identity_adjudication,
    preview_carried_source_identity_adjudication,
)
from app.services.owner_commands import CommandContext

CONFIRMATION = "RECORD_REVIEWED_NATIVE_BEFORE_HANDOFF"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _required(parser: argparse.ArgumentParser, value: str | None, flag: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        parser.error(f"{flag} is required with --apply")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=_uuid, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--fingerprint")
    parser.add_argument("--evidence-ref")
    parser.add_argument("--evidence-sha256")
    parser.add_argument("--reviewed-by-id", type=_uuid)
    parser.add_argument("--approved-by-id", type=_uuid)
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--idempotency-key")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    with SessionLocal() as db:
        preview = preview_carried_source_identity_adjudication(db, args.account_id)
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "read_only",
                    "account_id": str(preview.account_id),
                    "eligible": preview.eligible,
                    "disposition": (
                        preview.disposition.value
                        if preview.disposition is not None
                        else None
                    ),
                    "account_created_at": preview.account_created_at.isoformat(),
                    "financial_handoff_at": preview.financial_handoff_at.isoformat(),
                    "source_system": preview.source_system,
                    "crm_reference_kinds": list(preview.crm_reference_kinds),
                    "splynx_service_evidence_count": (
                        preview.splynx_service_evidence_count
                    ),
                    "splynx_invoice_evidence_count": (
                        preview.splynx_invoice_evidence_count
                    ),
                    "splynx_payment_evidence_count": (
                        preview.splynx_payment_evidence_count
                    ),
                    "blockers": [item.value for item in preview.blockers],
                    "fingerprint": preview.fingerprint,
                    "existing_decision_id": (
                        str(preview.existing_decision_id)
                        if preview.existing_decision_id is not None
                        else None
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not args.apply:
            print("DRY RUN — no changes written.")
            return 0
        if args.confirm != CONFIRMATION:
            parser.error(f"--confirm must be exactly {CONFIRMATION}")
        if not preview.eligible:
            parser.error("the current provenance preview is not eligible")
        fingerprint = _required(parser, args.fingerprint, "--fingerprint")
        if fingerprint != preview.fingerprint:
            parser.error("--fingerprint does not match the current preview")
        evidence_ref = _required(parser, args.evidence_ref, "--evidence-ref")
        evidence_sha256 = _required(parser, args.evidence_sha256, "--evidence-sha256")
        actor = _required(parser, args.actor, "--actor")
        reason = _required(parser, args.reason, "--reason")
        idempotency_key = _required(parser, args.idempotency_key, "--idempotency-key")
        if args.reviewed_by_id is None or args.approved_by_id is None:
            parser.error(
                "--reviewed-by-id and --approved-by-id are required with --apply"
            )

        db.rollback()
        outcome = confirm_carried_source_identity_adjudication(
            db,
            ConfirmCarriedSourceIdentityCommand(
                context=CommandContext.system(
                    actor=actor,
                    scope=OWNER,
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                account_id=args.account_id,
                expected_preview_fingerprint=fingerprint,
                evidence_ref=evidence_ref,
                evidence_sha256=evidence_sha256,
                reviewed_by_id=args.reviewed_by_id,
                approved_by_id=args.approved_by_id,
            ),
        )
        print(
            json.dumps(
                {
                    "decision_id": str(outcome.decision_id),
                    "account_id": str(outcome.account_id),
                    "disposition": outcome.disposition.value,
                    "preview_fingerprint": outcome.preview_fingerprint,
                    "replayed": outcome.replayed,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
