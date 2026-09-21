"""Admin adapter mapping for scoped field-note read authorization."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.auth_dependencies import grant_scopes_for_permission
from app.services.field.note_commands import StaffFieldNoteAccess

FIELD_NOTE_READ_PERMISSION = "operations:dispatch:read"


def resolve_staff_field_note_access(
    db: Session, auth: Mapping[str, object] | None
) -> StaffFieldNoteAccess:
    """Convert permission-gate evidence into the owner's typed read scope."""

    if auth is None:
        return StaffFieldNoteAccess()
    decision = grant_scopes_for_permission(dict(auth), db, FIELD_NOTE_READ_PERMISSION)
    if decision == "global":
        return StaffFieldNoteAccess(global_access=True)
    if not isinstance(decision, set):
        return StaffFieldNoteAccess()
    reseller_ids: list[UUID] = []
    regions: list[str] = []
    for scope_type, scope_id in sorted(decision):
        if scope_type == "reseller":
            try:
                reseller_ids.append(UUID(scope_id))
            except (TypeError, ValueError):
                continue
        elif scope_type == "region" and scope_id.strip():
            regions.append(scope_id.strip())
    return StaffFieldNoteAccess(
        reseller_ids=tuple(dict.fromkeys(reseller_ids)),
        regions=tuple(dict.fromkeys(regions)),
    )


__all__ = ["FIELD_NOTE_READ_PERMISSION", "resolve_staff_field_note_access"]
