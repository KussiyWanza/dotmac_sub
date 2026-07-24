#!/usr/bin/env python
"""Preview, gate, or apply legacy service-extension duplicate reconciliation.

Preview is the default and writes nothing. Apply requires the exact reviewed
fingerprint, explicit preservation of chained customer entitlement, an
idempotency key, actor, reason, and effective timestamp.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.service_extensions import (
    ChainedGrantResolution,
    ReconcileServiceExtensionDuplicatesCommand,
    ServiceExtensionDuplicateGroup,
    preview_service_extension_duplicate_reconciliation,
    reconcile_service_extension_duplicates,
)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _group_payload(group: ServiceExtensionDuplicateGroup) -> dict[str, object]:
    return {
        "extension_id": str(group.extension_id),
        "subscription_id": str(group.subscription_id),
        "subscriber_id": str(group.subscriber_id),
        "kind": group.kind.value,
        "extension_days": group.extension_days,
        "subscription_next_billing_at": (
            group.subscription_next_billing_at.isoformat()
            if group.subscription_next_billing_at
            else None
        ),
        "manual_review_reason": group.manual_review_reason,
        "entries": [
            {
                "entry_id": str(entry.entry_id),
                "previous_next_billing_at": (
                    entry.previous_next_billing_at.isoformat()
                    if entry.previous_next_billing_at
                    else None
                ),
                "new_next_billing_at": (
                    entry.new_next_billing_at.isoformat()
                    if entry.new_next_billing_at
                    else None
                ),
                "created_at": entry.created_at.isoformat(),
                "downstream_reference_count": entry.downstream_reference_count,
            }
            for entry in group.entries
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when duplicate identities exist; print summary only.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--effective-at", type=_timestamp)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument(
        "--preserve-chained-entitlement",
        action="store_true",
        help="Preserve chained grant time as a separately audited extension.",
    )
    args = parser.parse_args()

    if args.check and args.apply:
        parser.error("--check and --apply are mutually exclusive")
    if args.apply:
        missing = [
            name
            for name, value in (
                ("--fingerprint", args.fingerprint),
                ("--effective-at", args.effective_at),
                ("--idempotency-key", args.idempotency_key),
                ("--actor", args.actor),
                ("--reason", args.reason),
                (
                    "--preserve-chained-entitlement",
                    args.preserve_chained_entitlement,
                ),
            )
            if not value
        ]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
        with db_session_adapter.owner_command_session() as db:
            result = reconcile_service_extension_duplicates(
                db,
                ReconcileServiceExtensionDuplicatesCommand(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope="service_extension_duplicate_reconciliation",
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                    preview_fingerprint=args.fingerprint,
                    effective_at=args.effective_at,
                    chained_grant_resolution=(
                        ChainedGrantResolution.preserve_as_corrective_extension
                    ),
                ),
            )
        print(
            json.dumps(
                {
                    "applied": True,
                    "preview_fingerprint": result.preview_fingerprint,
                    "exact_duplicates_collapsed": (result.exact_duplicates_collapsed),
                    "chained_grants_preserved": result.chained_grants_preserved,
                    "replayed": result.replayed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    with db_session_adapter.read_session() as db:
        preview = preview_service_extension_duplicate_reconciliation(db)
    summary = {
        "candidate_count": len(preview.groups),
        "exact_duplicate_count": preview.exact_duplicate_count,
        "chained_grant_count": preview.chained_grant_count,
        "manual_review_count": preview.manual_review_count,
        "fingerprint": preview.fingerprint,
    }
    if args.check:
        print(json.dumps(summary, sort_keys=True))
        return 1 if preview.groups else 0
    print(
        json.dumps(
            {
                "dry_run": True,
                **summary,
                "groups": [_group_payload(group) for group in preview.groups],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
