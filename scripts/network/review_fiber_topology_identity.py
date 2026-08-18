"""Review staged point-asset identity without bypassing canonical owners."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.fiber_topology_identity import (  # noqa: E402
    FiberTopologyIdentityError,
    approve_identity_decision,
    decision_to_dict,
    decline_identity_decision,
    execute_identity_decision,
    finalize_identity_decision,
    propose_identity_decision,
    supersede_approved_identity_decision,
)
from app.services.network.fiber_topology_review import (  # noqa: E402
    FiberTopologyProposalBatchBlocked,
    FiberTopologyReviewError,
    attest_identity_batch,
    execute_identity_batch,
    inspect_identity_batch,
    list_identity_review_queue,
    preview_identity_proposal_batch,
    propose_identity_batch,
    reconcile_identity_change_requests,
)
from app.services.network.fiber_topology_staging import SOURCE_PROFILES  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Propose, independently approve, and execute staged fiber point-asset "
            "identity decisions. Create decisions emit fiber change requests; this "
            "command never approves those canonical mutations."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    queue = commands.add_parser(
        "queue", help="List latest point-source identities and canonical candidates."
    )
    queue.add_argument("--profile", choices=sorted(SOURCE_PROFILES))
    queue.add_argument(
        "--state",
        default="actionable",
        choices=(
            "actionable",
            "active",
            "all",
            "applied",
            "closed",
            "declined",
            "linked",
            "unreviewed",
        ),
    )
    queue.add_argument("--limit", type=int, default=100)
    queue.add_argument("--offset", type=int, default=0)
    queue.add_argument("--include-source-properties", action="store_true")

    for command, help_text in (
        ("preview-batch", "Validate and hash a proposal manifest without writing."),
        ("propose-batch", "Atomically persist a validated proposal manifest."),
    ):
        batch = commands.add_parser(command, help=help_text)
        batch.add_argument("--manifest", required=True, type=Path)
        batch.add_argument("--actor", required=True)

    inspect_batch = commands.add_parser(
        "inspect-batch", help="Inspect a proposal batch and its control evidence."
    )
    inspect_batch.add_argument("--batch-id", required=True)

    attest_batch = commands.add_parser(
        "attest-batch",
        help="Independently approve or decline one exact proposal-batch manifest.",
    )
    attest_batch.add_argument("--batch-id", required=True)
    attest_batch.add_argument("--expected-manifest-sha256", required=True)
    attest_batch.add_argument("--action", choices=("approve", "decline"), required=True)
    attest_batch.add_argument("--actor", required=True)
    attest_batch.add_argument("--notes", required=True)

    execute_batch = commands.add_parser(
        "execute-batch",
        help="Execute a bounded set of independently approved batch decisions.",
    )
    execute_batch.add_argument("--batch-id", required=True)
    execute_batch.add_argument("--expected-manifest-sha256", required=True)
    execute_batch.add_argument("--actor", required=True)
    execute_batch.add_argument("--limit", type=int, default=50)

    propose = commands.add_parser("propose", help="Propose one source identity action.")
    propose.add_argument("--feature-id", required=True)
    propose.add_argument(
        "--action", required=True, choices=("create", "link_existing", "reject")
    )
    propose.add_argument("--target-asset-id")
    propose.add_argument("--actor", required=True)
    propose.add_argument("--reason", required=True)

    approve = commands.add_parser(
        "approve", help="Independently approve a proposed identity action."
    )
    approve.add_argument("--decision-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--notes", required=True)

    decline = commands.add_parser(
        "decline", help="Decline a proposal while preserving its review evidence."
    )
    decline.add_argument("--decision-id", required=True)
    decline.add_argument("--actor", required=True)
    decline.add_argument("--notes", required=True)

    supersede = commands.add_parser(
        "supersede-approved",
        help=(
            "Supersede an approved, unexecuted identity decision after newer "
            "authoritative source evidence is staged."
        ),
    )
    supersede.add_argument("--decision-id", required=True)
    supersede.add_argument("--actor", required=True)
    supersede.add_argument("--reason", required=True)

    execute = commands.add_parser(
        "execute", help="Execute an approved link/rejection or emit a change request."
    )
    execute.add_argument("--decision-id", required=True)
    execute.add_argument("--actor", required=True)

    finalize = commands.add_parser(
        "finalize", help="Project an applied/rejected fiber change request outcome."
    )
    finalize.add_argument("--decision-id", required=True)
    finalize.add_argument("--actor", required=True)

    reconcile = commands.add_parser(
        "reconcile", help="Finalize applied/rejected change-request outcomes."
    )
    reconcile.add_argument("--actor", required=True)
    reconcile.add_argument("--limit", type=int, default=100)

    return parser.parse_args()


def _load_batch_manifest(path: Path) -> tuple[list[dict[str, Any]], str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FiberTopologyReviewError(f"cannot read proposal manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise FiberTopologyReviewError("proposal manifest must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise FiberTopologyReviewError(
            "proposal manifest items must be a list of objects"
        )
    reason = str(payload.get("reason") or "").strip()
    source_name = str(payload.get("source_name") or path.name).strip()
    return items, reason, source_name


def main() -> int:
    args = parse_args()
    exit_code = 0
    try:
        with SessionLocal() as db:
            if args.command == "queue":
                output = list_identity_review_queue(
                    db,
                    profile=args.profile,
                    state=args.state,
                    limit=args.limit,
                    offset=args.offset,
                    include_source_properties=args.include_source_properties,
                ).to_dict()
            elif args.command in {"preview-batch", "propose-batch"}:
                items, reason, source_name = _load_batch_manifest(args.manifest)
                if args.command == "preview-batch":
                    preview = preview_identity_proposal_batch(
                        db,
                        items,
                        proposed_by=args.actor,
                        reason=reason,
                        source_name=source_name,
                    )
                    output = preview.to_dict()
                    exit_code = 0 if preview.ready else 2
                else:
                    output = propose_identity_batch(
                        db,
                        items,
                        proposed_by=args.actor,
                        reason=reason,
                        source_name=source_name,
                    ).to_dict()
            elif args.command == "inspect-batch":
                output = inspect_identity_batch(db, args.batch_id)
            elif args.command == "attest-batch":
                output = attest_identity_batch(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    action=args.action,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                ).to_dict()
            elif args.command == "execute-batch":
                output = execute_identity_batch(
                    db,
                    args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    executed_by=args.actor,
                    limit=args.limit,
                ).to_dict()
            elif args.command == "propose":
                decision = propose_identity_decision(
                    db,
                    staged_feature_id=args.feature_id,
                    action=args.action,
                    target_asset_id=args.target_asset_id,
                    proposed_by=args.actor,
                    reason=args.reason,
                )
                output = decision_to_dict(decision)
            elif args.command == "approve":
                decision = approve_identity_decision(
                    db,
                    decision_id=args.decision_id,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                )
                output = decision_to_dict(decision)
            elif args.command == "decline":
                decision = decline_identity_decision(
                    db,
                    decision_id=args.decision_id,
                    reviewed_by=args.actor,
                    review_notes=args.notes,
                )
                output = decision_to_dict(decision)
            elif args.command == "supersede-approved":
                decision = supersede_approved_identity_decision(
                    db,
                    decision_id=args.decision_id,
                    superseded_by=args.actor,
                    reason=args.reason,
                )
                output = decision_to_dict(decision)
            elif args.command == "execute":
                decision = execute_identity_decision(
                    db,
                    decision_id=args.decision_id,
                    executed_by=args.actor,
                )
                output = decision_to_dict(decision)
            elif args.command == "finalize":
                decision = finalize_identity_decision(
                    db,
                    decision_id=args.decision_id,
                    finalized_by=args.actor,
                )
                output = decision_to_dict(decision)
            else:
                result = reconcile_identity_change_requests(
                    db, finalized_by=args.actor, limit=args.limit
                )
                output = result.to_dict()
                exit_code = 2 if result.errors else 0
    except FiberTopologyProposalBatchBlocked as exc:
        print(json.dumps(exc.preview.to_dict(), indent=2, sort_keys=True))
        return 2
    except (FiberTopologyIdentityError, FiberTopologyReviewError) as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(output, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
