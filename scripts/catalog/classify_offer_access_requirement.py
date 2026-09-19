"""Report unclassified offer versions, or preview/apply one reviewed
access-requirement classification.

Dry-run first, three modes:

    # 1. Default: deterministic, paginated worklist of unclassified rows.
    python -m scripts.catalog.classify_offer_access_requirement

    # 2. Targeted preview (no --apply): binds the exact fingerprint. If this
    #    exact transition was already applied, the preview reflects that
    #    recorded outcome instead of erroring — reusing the SAME
    #    --idempotency-key at --apply time then replays it.
    python -m scripts.catalog.classify_offer_access_requirement \\
        --offer-version-id ... --proposed network_access \\
        --review-reference JIRA-1234

    # 3. Apply: requires the exact fingerprint from step 2, the operator's
    #    claimed staff-principal identity (verified against real RBAC
    #    grants — see below), and an idempotency key.
    python -m scripts.catalog.classify_offer_access_requirement \\
        --offer-version-id ... --proposed network_access \\
        --review-reference JIRA-1234 --apply \\
        --expected-preview-fingerprint <sha256> \\
        --actor-system-user-id <uuid> \\
        --reason "confirmed with network ops" --idempotency-key <key> --confirm

``--actor-system-user-id`` is the operator's CLAIMED identity, never a
free-text label: it is the ONLY identity input (there is no separate
``--actor`` argument to type a different display name into), and it is
resolved against real RBAC grants via ``has_permission``/
``system_user_role_names`` — re-verified fresh, INSIDE the command's own
transaction, immediately before the write — before the owner ever treats the
action as authorized. This CLI does not itself verify who is really typing
the command: host or container shell access to run it at all is this
script's authentication boundary, the same trust model documented in
``scripts/billing/correct_customer_subledger_opening.py`` and its siblings
(``scripts/support/issue_inbox_completion_override.py``,
``scripts/billing/reconcile_prepaid_drafts.py``,
``scripts/billing/repair_prepaid_funding_consequences.py``). What IS
guaranteed: the string recorded as ``classified_by``/audit actor/event actor
is always derived from the RBAC-verified id (``principal_label``), never
from an unverified claim, and a claimed id that does not hold the
``catalog:offer_access_requirement:classify`` permission (or an admin/``*``
wildcard grant) is refused.
"""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from app.models.catalog import AccessRequirement
from app.services.catalog.offer_access_requirement import (
    CLASSIFY_PERMISSION,
    ClassifyOfferAccessRequirementCommand,
    PreviewClassifyOfferAccessRequirementQuery,
    classify_offer_version_access_requirement,
    list_unclassified_offer_versions,
    preview_classify_offer_version_access_requirement,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offer-version-id", type=UUID)
    parser.add_argument(
        "--proposed",
        choices=[
            value.value
            for value in (
                AccessRequirement.network_access,
                AccessRequirement.no_network_access,
            )
        ],
    )
    parser.add_argument("--review-reference")
    parser.add_argument("--reason")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-preview-fingerprint")
    parser.add_argument("--command-id", type=UUID)
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    return parser


def _worklist_dict(limit: int, offset: int) -> dict[str, object]:
    with db_session_adapter.read_session() as db:
        worklist = list_unclassified_offer_versions(db, limit=limit, offset=offset)
    return {
        "mode": "worklist",
        "total_count": worklist.total_count,
        "limit": worklist.limit,
        "offset": worklist.offset,
        "rows": [
            {
                "offer_version_id": str(row.offer_version_id),
                "offer_id": str(row.offer_id),
                "version_number": row.version_number,
                "name": row.name,
                "created_at": row.created_at.isoformat(),
            }
            for row in worklist.rows
        ],
    }


def _preview_dict(preview) -> dict[str, object]:  # noqa: ANN001
    return {
        "offer_version_id": str(preview.offer_version_id),
        "current_access_requirement": preview.current_access_requirement.value,
        "proposed_access_requirement": preview.proposed_access_requirement.value,
        "row_updated_at": preview.row_updated_at.isoformat(),
        "review_reference": preview.review_reference,
        "preview_fingerprint": preview.preview_fingerprint,
        "already_applied": preview.already_applied,
    }


def main() -> int:
    args = _parser().parse_args()

    if args.offer_version_id is None:
        print(json.dumps(_worklist_dict(args.limit, args.offset), sort_keys=True))
        return 0

    if not args.proposed or not args.review_reference:
        print(
            json.dumps(
                {
                    "error": (
                        "--offer-version-id requires --proposed and --review-reference"
                    )
                },
                sort_keys=True,
            )
        )
        return 2

    query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=args.offer_version_id,
        proposed_access_requirement=AccessRequirement(args.proposed),
        review_reference=args.review_reference,
    )
    try:
        with db_session_adapter.read_session() as db:
            preview = preview_classify_offer_version_access_requirement(db, query)

        if not args.apply:
            print(
                json.dumps(
                    {"applied": False, "preview": _preview_dict(preview)},
                    sort_keys=True,
                )
            )
            return 0

        if not all(
            (
                args.confirm,
                args.expected_preview_fingerprint,
                args.command_id,
                args.actor_system_user_id,
                args.reason,
                args.idempotency_key,
            )
        ):
            print(
                json.dumps(
                    {
                        "applied": False,
                        "error": (
                            "--apply requires --confirm, --expected-preview-"
                            "fingerprint, --command-id, --actor-system-user-id, "
                            "--reason, and --idempotency-key"
                        ),
                        "preview": _preview_dict(preview),
                    },
                    sort_keys=True,
                )
            )
            return 2

        with db_session_adapter.owner_command_session() as db:
            outcome = classify_offer_version_access_requirement(
                db,
                ClassifyOfferAccessRequirementCommand(
                    context=CommandContext(
                        command_id=args.command_id,
                        correlation_id=args.command_id,
                        # A placeholder; the service records the AUTHENTICATED
                        # principal (derived from authorized_system_user_id)
                        # as the actor of record, never this string.
                        actor=f"system_user:{args.actor_system_user_id}",
                        scope=CLASSIFY_PERMISSION,
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                    query=query,
                    expected_preview_fingerprint=args.expected_preview_fingerprint,
                    authorized_system_user_id=args.actor_system_user_id,
                ),
            )
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
                "offer_version_id": str(outcome.offer_version_id),
                "previous_access_requirement": (
                    outcome.previous_access_requirement.value
                ),
                "new_access_requirement": outcome.new_access_requirement.value,
                "replayed": outcome.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
