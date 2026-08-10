"""Guarded Ticket.status transitions (SM-gap #41)."""

import pytest

from app.models.support import Ticket, TicketStatus
from app.services.support import transition_ticket_status


def test_rejects_garbage_status():
    t = Ticket(status="open")
    with pytest.raises(ValueError):
        transition_ticket_status(t, "not-a-real-status", source="test")
    assert t.status == "open"


def test_terminal_closed_not_reopened_by_crm_or_automation():
    # the active bug: CRM pull / automation must not resurrect a closed ticket
    for source in ("crm_pull", "automation"):
        t = Ticket(status="closed")
        changed = transition_ticket_status(t, "open", source=source)
        assert changed is False
        assert t.status == "closed"


def test_merged_and_canceled_are_terminal():
    for terminal in ("merged", "canceled"):
        t = Ticket(status=terminal)
        assert transition_ticket_status(t, "open", source="crm_pull") is False
        assert t.status == terminal


def test_admin_may_reopen_with_allow_reopen():
    t = Ticket(status="closed")
    changed = transition_ticket_status(
        t, "open", source="admin_update", allow_reopen=True
    )
    assert changed is True
    assert t.status == "open"


def test_normal_forward_transition():
    t = Ticket(status="open")
    assert transition_ticket_status(t, TicketStatus.closed, source="admin") is True
    assert t.status == "closed"


def test_legacy_resolved_input_is_canonicalized_to_closed():
    t = Ticket(status="open")
    assert transition_ticket_status(t, "resolved", source="legacy_api") is True
    assert t.status == "closed"


def test_legacy_stored_resolved_is_repaired_and_cannot_be_reopened():
    t = Ticket(status="resolved")

    assert transition_ticket_status(t, "open", source="crm_pull") is False
    assert t.status == "closed"


def test_same_status_is_noop():
    t = Ticket(status="open")
    assert transition_ticket_status(t, "open", source="admin") is False


def test_new_ticket_takes_status_as_is():
    # current is None (fresh ticket) — not terminal, so CRM status applies
    t = Ticket()
    assert transition_ticket_status(t, "closed", source="crm_pull") is True
    assert t.status == "closed"
