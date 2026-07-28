"""Boundary guards for the identity & onboarding chain (rank 5).

The chain contract (docs/designs/IDENTITY_ONBOARDING_CHAIN.md): the
provisioning→invite hops stay evented and fail-closed; capability TTL
authority stays with the calling domain; the ticket SLA breach deadline is
a durable per-clock timer with a receipted consumer.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_invite_and_recovery_handlers_fail_closed():
    for relative in (
        "app/services/events/handlers/staff_invite.py",
        "app/services/events/handlers/reseller_invite.py",
        "app/services/events/handlers/password_recovery.py",
    ):
        src = _source(relative)
        # Recipient identity drift raises; a failed intent stays a failed
        # retryable delivery, never a warning log.
        assert "except Exception" not in src
        assert "sha256" in src or "digest" in src


def test_enrollment_capability_ttl_stays_domain_owned():
    src = _source("app/services/customer_credential_enrollment.py")
    # The redeem path bounds token lifetime against the configured TTL —
    # an over-long token is rejected even when correctly signed.
    assert "expires_at" in src and "issued_at" in src
    signing = _source("app/services/context_signing.py")
    # Token signing owns envelope and signature only: no domain TTL
    # settings resolve inside it.
    assert "user_invite_expiry_minutes" not in signing
    assert "password_reset_expiry_minutes" not in signing


def test_sla_breach_deadline_is_a_durable_timer_with_receipted_consumer():
    sla = _source("app/services/sla_assignment.py")
    assert 'purpose="sla_breach_due"' in sla
    assert 'output_event_type="support.ticket_sla_breach_due"' in sla
    support = _source("app/services/support.py")
    assert "def consume_sla_breach_due" in support
    handler = _source("app/services/events/handlers/support_lifecycle_projection.py")
    assert '"support.ticket_sla_breach_due"' in handler


def test_pending_subscriber_role_widens_only_through_party_owner():
    src = _source("app/services/party.py")
    # ensure_role permits exactly the pending -> active widening; identity
    # flows must not gain a parallel role writer.
    assert "PartyInvariantError" in src
    enrollment = _source("app/services/customer_credential_enrollment.py")
    assert "ensure_role" not in enrollment


def test_invitation_aggregate_is_lifecycle_evidence_with_durable_expiry():
    svc = _source("app/services/access_invitations.py")
    assert 'purpose="invitation_expiry_due"' in svc
    assert "consume_owner_output" in svc
    # Rows are evidence, never a grant: the module writes no credentials
    # and no sessions.
    assert "password_hash" not in svc
    assert "AuthSession" not in svc
    # Every invite issuance path records its invitation.
    for producer in (
        "app/services/staff_provisioning.py",
        "app/services/reseller_onboarding.py",
        "app/services/web_system_user_mutations.py",
    ):
        assert "access_invitations.record_issued" in _source(producer)
    # A completed reset stamps acceptance.
    assert "access_invitations.mark_accepted" in _source(
        "app/services/credential_recovery.py"
    )


def test_cx_acceptance_deadline_is_a_durable_timer():
    cx = _source("app/services/customer_experience_handoffs.py")
    assert 'purpose="cx_acceptance_due"' in cx
    assert "def consume_cx_acceptance_due" in cx
    assert "def consume_service_order_completion" in cx
    handler = _source("app/services/events/handlers/sales_lifecycle_projection.py")
    assert '"sales.cx_acceptance_due"' in handler
