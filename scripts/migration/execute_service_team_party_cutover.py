#!/usr/bin/env python3
"""Execute one exact, separately approved service-team Party cutover plan.

Both artifacts are private and digest-bound.  Execution requires an explicit
acknowledgement and a named actor.  Output contains only hashes and aggregate
counts; the owner transaction records the durable PII-free receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.exc import SQLAlchemyError

from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.service_team_party_cutover import (
    AdoptServiceTeamPartyCutover,
    ServiceTeamPartyCutoverApproval,
    ServiceTeamPartyCutoverError,
    ServiceTeamPartyCutoverPlan,
    adopt_service_team_party_cutover,
)


class ArtifactError(ValueError):
    """Raised when a protected execution artifact cannot be trusted."""


def _private_json(path: Path, *, label: str) -> tuple[object, str]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ArtifactError(f"{label} cannot be read") from exc
    if mode & 0o077:
        raise ArtifactError(f"{label} must not be accessible by group or others")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactError(f"{label} cannot be read") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{label} must contain valid UTF-8 JSON") from exc
    return payload, hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Acknowledge that the approved plan may write production identity state.",
    )
    args = parser.parse_args()
    if not args.execute:
        parser.error(
            "--execute is required; use the audit and planner for read-only work"
        )
    try:
        plan_payload, plan_file_sha256 = _private_json(args.plan, label="plan file")
        approval_payload, approval_file_sha256 = _private_json(
            args.approval,
            label="approval file",
        )
        plan = ServiceTeamPartyCutoverPlan.from_payload(plan_payload)
        approval = ServiceTeamPartyCutoverApproval.from_payload(approval_payload)
        command_id = uuid5(
            NAMESPACE_URL,
            f"dotmac:service-team-party-cutover:{plan.plan_digest}",
        )
        with db_session_adapter.owner_command_session() as db:
            outcome = adopt_service_team_party_cutover(
                db,
                AdoptServiceTeamPartyCutover(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope="service_team_party_cutover:adopt",
                        reason=approval.reason,
                        command_id=command_id,
                        correlation_id=command_id,
                        idempotency_key=plan.plan_digest,
                    ),
                    plan=plan,
                    approval=approval,
                    plan_file_sha256=plan_file_sha256,
                    approval_file_sha256=approval_file_sha256,
                ),
            )
    except (ArtifactError, ServiceTeamPartyCutoverError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    except SQLAlchemyError:
        print("FAILED: database execution failed and the transaction was rolled back")
        return 1
    print(
        json.dumps(
            {
                "status": "replayed" if outcome.replayed else "applied",
                "plan_digest": outcome.plan_digest,
                "parties_created": outcome.parties_created,
                "principals_bound": outcome.principals_bound,
                "memberships_created": outcome.memberships_created,
                "replayed": outcome.replayed,
                "credential_changes": 0,
                "rbac_changes": 0,
                "team_lifecycle_changes": 0,
                "manager_changes": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
