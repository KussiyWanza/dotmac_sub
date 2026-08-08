"""Preview or repair staff email/local-credential username drift.

Dry-run is the default. Apply mode accepts only the exact fingerprint printed
by the reviewed preview and repairs unambiguous missing, username, and
activation drift through the canonical owner command. No email addresses or
credential usernames are printed.

Examples::

    python -m scripts.one_off.reconcile_staff_login_identities
    python -m scripts.one_off.reconcile_staff_login_identities --apply \
      --expected-fingerprint <reviewed-sha256> --actor user:<uuid> \
      --reason "reviewed staff login identity repair" \
      --idempotency-prefix staff-login-repair-2026-08-06
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.services import staff_provisioning
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


@dataclass(frozen=True)
class ReconciliationPlan:
    drift: tuple[staff_provisioning.StaffLoginIdentityDrift, ...]
    fingerprint: str

    @property
    def drift_user_count(self) -> int:
        return len({item.user_id for item in self.drift})

    @property
    def repairable(self) -> tuple[staff_provisioning.StaffLoginIdentityDrift, ...]:
        blocked_issues = {
            staff_provisioning.StaffLoginIdentityIssue.multiple_credentials,
            staff_provisioning.StaffLoginIdentityIssue.username_conflict,
        }
        blocked_users = {
            item.user_id for item in self.drift if item.issue in blocked_issues
        }
        repairable_issues = {
            staff_provisioning.StaffLoginIdentityIssue.missing_credential,
            staff_provisioning.StaffLoginIdentityIssue.username_mismatch,
            staff_provisioning.StaffLoginIdentityIssue.activation_mismatch,
        }
        by_user: dict[UUID, staff_provisioning.StaffLoginIdentityDrift] = {}
        for item in sorted(
            self.drift,
            key=lambda candidate: (str(candidate.user_id), candidate.issue.value),
        ):
            if item.user_id in blocked_users or item.issue not in repairable_issues:
                continue
            by_user.setdefault(item.user_id, item)
        return tuple(by_user.values())

    @property
    def blocked_user_count(self) -> int:
        return self.drift_user_count - len(self.repairable)


def build_plan(
    drift: tuple[staff_provisioning.StaffLoginIdentityDrift, ...],
) -> ReconciliationPlan:
    canonical = "\n".join(
        f"{item.user_id}|{item.issue.value}|{item.email_sha256}"
        for item in sorted(
            drift,
            key=lambda item: (str(item.user_id), item.issue.value),
        )
    )
    return ReconciliationPlan(
        drift=drift,
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def load_plan() -> ReconciliationPlan:
    with SessionLocal() as db:
        return build_plan(staff_provisioning.list_staff_login_identity_drift(db))


def apply_plan(
    plan: ReconciliationPlan,
    *,
    expected_fingerprint: str,
    actor: str,
    reason: str,
    idempotency_prefix: str,
) -> int:
    if plan.fingerprint != expected_fingerprint:
        raise ValueError(
            "Drift changed after review; run preview again and approve its fingerprint."
        )
    repaired = 0
    for item in plan.repairable:
        command_id = uuid4()
        with SessionLocal() as db:
            outcome = staff_provisioning.reconcile_staff_login_identity(
                db,
                staff_provisioning.ReconcileStaffLoginIdentityCommand(
                    context=CommandContext(
                        command_id=command_id,
                        correlation_id=command_id,
                        actor=actor,
                        scope=staff_provisioning.STAFF_ASSIGN_SCOPE,
                        reason=reason,
                        idempotency_key=(
                            f"{idempotency_prefix}:{item.user_id}:{item.email_sha256}"
                        ),
                    ),
                    user_id=item.user_id,
                    expected_email_sha256=item.email_sha256,
                ),
            )
            repaired += int(outcome.changed)
    return repaired


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Repair reviewed unambiguous staff login identity drift.",
    )
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--actor", help="Audit actor, for example user:<uuid>.")
    parser.add_argument("--reason")
    parser.add_argument("--idempotency-prefix")
    return parser


def main() -> int:
    args = _parser().parse_args()
    plan = load_plan()
    counts = Counter(item.issue.value for item in plan.drift)
    print(f"drift_count={len(plan.drift)}")
    print(f"drift_user_count={plan.drift_user_count}")
    print(f"repairable_count={len(plan.repairable)}")
    for issue in staff_provisioning.StaffLoginIdentityIssue:
        print(f"{issue.value}={counts[issue.value]}")
    print(f"fingerprint={plan.fingerprint}")

    if not args.apply:
        print("mode=preview")
        return 0
    missing = tuple(
        name
        for name, value in (
            ("--expected-fingerprint", args.expected_fingerprint),
            ("--actor", args.actor),
            ("--reason", args.reason),
            ("--idempotency-prefix", args.idempotency_prefix),
        )
        if not value
    )
    if missing:
        raise ValueError(f"Apply mode requires: {', '.join(missing)}")
    repaired = apply_plan(
        plan,
        expected_fingerprint=args.expected_fingerprint,
        actor=args.actor,
        reason=args.reason,
        idempotency_prefix=args.idempotency_prefix,
    )
    print("mode=apply")
    print(f"repaired_count={repaired}")
    print(f"blocked_count={plan.blocked_user_count}")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except (DomainError, ValueError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        print(f"error={message}", file=sys.stderr)
        exit_code = 2
    raise SystemExit(exit_code)
