"""Shared helpers for OLT CRUD service modules."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.network import OltCardPort
from app.services.network.pon_port_identity import (
    canonical_name,
    derive_from_card_port,
)

_CANONICAL_PON_NAME_RE = re.compile(r"^\d+/\d+/\d+$")


def canonical_pon_name_from_card_port(
    db: Session,
    card_port: OltCardPort,
) -> str:
    """Canonical ``frame/slot/port`` name for a card port.

    Thin adapter over ``network.pon_port_identity``. It previously fell back to
    the card port's own display name and then to ``pon-{port_number}`` when the
    shelf or card was missing -- so a gap in hardware topology silently became
    a PON identity. Both fallbacks are gone: an underivable card port now
    raises, because a port number without its frame and slot names nothing.
    """
    return canonical_name(derive_from_card_port(db, card_port))


def parse_canonical_pon_name(name: str | None) -> tuple[str, int] | None:
    text = str(name or "").strip()
    if not _CANONICAL_PON_NAME_RE.fullmatch(text):
        return None
    board, port = text.rsplit("/", 1)
    return board, int(port)
