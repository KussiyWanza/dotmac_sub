"""Request and review immutable ONT constraint-cutover authorization evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal, begin_read_only_snapshot  # noqa: E402
from app.services.network.ont_assignment_constraint_authorization import (  # noqa: E402
    OntAssignmentConstraintAuthorizationError,
    OntAssignmentConstraintAuthorizationRequestBlocked,
    OntAssignmentConstraintAuthorizationReviewBlocked,
    inspect_ont_assignment_constraint_authorizations,
    preview_ont_assignment_constraint_authorization_request,
    preview_ont_assignment_constraint_authorization_review,
    request_ont_assignment_constraint_authorization,
    review_ont_assignment_constraint_authorization,
)


def _request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-coverage-report-sha256", required=True)
    parser.add_argument("--expected-cutover-report-sha256", required=True)
    parser.add_argument("--target-environment", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)


def _review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--authorization-request-id", required=True)
    parser.add_argument("--expected-request-sha256", required=True)
    parser.add_argument("--action", choices=("approve", "decline"), required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--notes", required=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bind a named target and expiry to one exact clean ONT coverage "
            "snapshot, then independently approve or decline the immutable "
            "request. This command cannot run or generate constraint DDL."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    request_preview = commands.add_parser(
        "request-preview", help="Build exact request evidence without writing."
    )
    _request_arguments(request_preview)

    request = commands.add_parser(
        "request", help="Persist the exact previewed authorization request."
    )
    _request_arguments(request)
    request.add_argument("--expected-request-sha256", required=True)

    review_preview = commands.add_parser(
        "review-preview", help="Recheck current evidence without writing."
    )
    _review_arguments(review_preview)

    review = commands.add_parser(
        "review", help="Persist an independent approve/decline attestation."
    )
    _review_arguments(review)
    review.add_argument("--expected-attestation-sha256", required=True)

    inspect = commands.add_parser(
        "inspect", help="Project current, stale, expired, and declined evidence."
    )
    inspect.add_argument("--target-environment")
    return parser.parse_args()


def _configure_snapshot(db, *, read_only: bool) -> None:
    """Pin the snapshot, and make a preview structurally unable to write.

    Only the read-only half was broken: the write commands already asked
    for their isolation level as an execution option, while the preview
    commands issued `SET TRANSACTION READ ONLY` after the operator-tenant
    hook had already run a statement.
    """

    if db.get_bind().dialect.name != "postgresql":
        return
    if read_only:
        begin_read_only_snapshot(db)
        return
    db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def main() -> int:
    args = parse_args()
    try:
        with SessionLocal() as db:
            _configure_snapshot(
                db,
                read_only=args.command
                in {"request-preview", "review-preview", "inspect"},
            )
            if args.command in {"request-preview", "request"}:
                kwargs = {
                    "expected_coverage_report_sha256": (
                        args.expected_coverage_report_sha256
                    ),
                    "expected_cutover_report_sha256": (
                        args.expected_cutover_report_sha256
                    ),
                    "target_environment": args.target_environment,
                    "expires_at": args.expires_at,
                    "requested_by": args.actor,
                    "reason": args.reason,
                }
                if args.command == "request-preview":
                    request_preview = (
                        preview_ont_assignment_constraint_authorization_request(
                            db, **kwargs
                        )
                    )
                    result = request_preview.to_dict()
                    exit_code = 0 if request_preview.ready else 2
                else:
                    result = request_ont_assignment_constraint_authorization(
                        db,
                        expected_request_sha256=args.expected_request_sha256,
                        **kwargs,
                    ).to_dict()
                    exit_code = 0
            elif args.command in {"review-preview", "review"}:
                kwargs = {
                    "expected_request_sha256": args.expected_request_sha256,
                    "action": args.action,
                    "reviewed_by": args.actor,
                    "review_notes": args.notes,
                }
                if args.command == "review-preview":
                    review_preview = (
                        preview_ont_assignment_constraint_authorization_review(
                            db,
                            args.authorization_request_id,
                            **kwargs,
                        )
                    )
                    result = review_preview.to_dict()
                    exit_code = 0 if review_preview.ready else 2
                else:
                    result = review_ont_assignment_constraint_authorization(
                        db,
                        args.authorization_request_id,
                        expected_attestation_sha256=(args.expected_attestation_sha256),
                        **kwargs,
                    ).to_dict()
                    exit_code = 0
            else:
                result = inspect_ont_assignment_constraint_authorizations(
                    db, target_environment=args.target_environment
                )
                exit_code = 0
    except (
        OntAssignmentConstraintAuthorizationRequestBlocked,
        OntAssignmentConstraintAuthorizationReviewBlocked,
    ) as exc:
        print(json.dumps(exc.preview.to_dict(), indent=2, sort_keys=True))
        return 2
    except OntAssignmentConstraintAuthorizationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
