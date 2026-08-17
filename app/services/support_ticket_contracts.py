"""Typed cross-owner contracts for authoritative support-ticket facts."""

from __future__ import annotations

from enum import Enum


class InternalOperationalTicketSource(str, Enum):
    """Approved internal sources allowed to request silent Ticket creation."""

    unmatched_radio_queue = "unmatched_radio_queue"
