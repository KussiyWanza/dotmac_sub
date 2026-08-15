"""Controlled CRM Network Map point-asset reconciliation workflow.

This command never snapshots CRM, never restores archives, and never stages
source observations. It consumes existing Selfcare staging evidence and keeps
proposal generation, review, dry-run apply, and approved apply as separate
manual operations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import read_only_snapshot_session  # noqa: E402
from app.services.network.crm_network_map_point_migration import (  # noqa: E402
    CrmNetworkMapPointMigrationError,
    build_crm_point_migration_report,
    dry_run_crm_point_identity_apply,
    execute_crm_point_identity_apply,
    preview_crm_point_identity_proposals,
    propose_crm_point_identity_proposals,
    select_authoritative_crm_point_batches,
)
from app.services.network.fiber_topology_review import (  # noqa: E402
    FiberTopologyProposalBatchBlocked,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select authoritative staged CRM point assets, report reconciliation, "
            "and manually drive governed identity proposals."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser(
        "report", help="Read-only authoritative-batch and reconciliation report."
    )
    report.add_argument("--expected-archive-sha256")
    report.add_argument("--include-rows", action="store_true")

    select_cmd = commands.add_parser(
        "select", help="Read-only authoritative batch selection evidence."
    )
    select_cmd.add_argument("--expected-archive-sha256")

    for proposal in (
        commands.add_parser(
            "preview-proposals",
            help="Validate and hash a proposal manifest without writing.",
        ),
        commands.add_parser(
            "propose-batch",
            help="Persist a proposal batch; never approves or applies it.",
        ),
    ):
        proposal.add_argument("--expected-archive-sha256", required=True)
        proposal.add_argument("--actor", required=True)
        proposal.add_argument("--reason", required=True)

    dry_run = commands.add_parser(
        "dry-run-apply",
        help="Inspect approved decisions that would execute; writes nothing.",
    )
    dry_run.add_argument("--batch-id", required=True)
    dry_run.add_argument("--expected-archive-sha256", required=True)

    apply_cmd = commands.add_parser(
        "apply-approved",
        help="Execute a bounded approved batch after exact archive/manifest checks.",
    )
    apply_cmd.add_argument("--batch-id", required=True)
    apply_cmd.add_argument("--expected-manifest-sha256", required=True)
    apply_cmd.add_argument("--expected-archive-sha256", required=True)
    apply_cmd.add_argument("--actor", required=True)
    apply_cmd.add_argument("--limit", type=int, default=50)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with read_only_snapshot_session() as db:
            if args.command == "report":
                output = build_crm_point_migration_report(
                    db,
                    expected_archive_sha256=args.expected_archive_sha256,
                    include_rows=args.include_rows,
                )
            elif args.command == "select":
                output = {
                    "selections": [
                        selection.to_dict()
                        for selection in select_authoritative_crm_point_batches(
                            db,
                            expected_archive_sha256=args.expected_archive_sha256,
                        )
                    ]
                }
            elif args.command == "preview-proposals":
                output = preview_crm_point_identity_proposals(
                    db,
                    expected_archive_sha256=args.expected_archive_sha256,
                    proposed_by=args.actor,
                    reason=args.reason,
                ).to_dict()
            elif args.command == "propose-batch":
                output = propose_crm_point_identity_proposals(
                    db,
                    expected_archive_sha256=args.expected_archive_sha256,
                    proposed_by=args.actor,
                    reason=args.reason,
                ).to_dict()
            elif args.command == "dry-run-apply":
                output = dry_run_crm_point_identity_apply(
                    db,
                    proposal_batch_id=args.batch_id,
                    expected_archive_sha256=args.expected_archive_sha256,
                )
            else:
                output = execute_crm_point_identity_apply(
                    db,
                    proposal_batch_id=args.batch_id,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    expected_archive_sha256=args.expected_archive_sha256,
                    executed_by=args.actor,
                    limit=args.limit,
                ).to_dict()
    except FiberTopologyProposalBatchBlocked as exc:
        print(json.dumps(exc.preview.to_dict(), indent=2, sort_keys=True))
        return 2
    except CrmNetworkMapPointMigrationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
