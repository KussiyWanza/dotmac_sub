"""Safety-contract tests for the staff login drift repair command."""

from uuid import uuid4

import pytest

from app.services import staff_provisioning
from scripts.one_off import reconcile_staff_login_identities


def test_reconciliation_fingerprint_is_order_independent() -> None:
    first = staff_provisioning.StaffLoginIdentityDrift(
        user_id=uuid4(),
        issue=staff_provisioning.StaffLoginIdentityIssue.username_mismatch,
        email_sha256="a" * 64,
    )
    second = staff_provisioning.StaffLoginIdentityDrift(
        user_id=uuid4(),
        issue=staff_provisioning.StaffLoginIdentityIssue.activation_mismatch,
        email_sha256="b" * 64,
    )

    forward = reconcile_staff_login_identities.build_plan((first, second))
    reverse = reconcile_staff_login_identities.build_plan((second, first))

    assert forward.fingerprint == reverse.fingerprint
    assert {item.user_id for item in forward.repairable} == {
        first.user_id,
        second.user_id,
    }


def test_apply_rejects_unreviewed_fingerprint() -> None:
    plan = reconcile_staff_login_identities.build_plan(())

    with pytest.raises(ValueError, match="Drift changed after review"):
        reconcile_staff_login_identities.apply_plan(
            plan,
            expected_fingerprint="0" * 64,
            actor=f"user:{uuid4()}",
            reason="reviewed staff login identity repair",
            idempotency_prefix="staff-login-repair",
        )


def test_conflict_blocks_otherwise_repairable_drift_for_same_user() -> None:
    user_id = uuid4()
    email_digest = "c" * 64
    plan = reconcile_staff_login_identities.build_plan(
        (
            staff_provisioning.StaffLoginIdentityDrift(
                user_id=user_id,
                issue=staff_provisioning.StaffLoginIdentityIssue.missing_credential,
                email_sha256=email_digest,
            ),
            staff_provisioning.StaffLoginIdentityDrift(
                user_id=user_id,
                issue=staff_provisioning.StaffLoginIdentityIssue.username_conflict,
                email_sha256=email_digest,
            ),
        )
    )

    assert plan.repairable == ()
    assert plan.drift_user_count == 1
    assert plan.blocked_user_count == 1
