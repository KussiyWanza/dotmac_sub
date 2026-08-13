"""Temporary field-API compatibility for the retired CRM identifier label."""

from __future__ import annotations

from fastapi import HTTPException


def resolve_work_order_id(
    *, work_order_id: str | None, crm_work_order_id: str | None
) -> str | None:
    """Resolve canonical and legacy inputs, rejecting ambiguous dual values."""

    canonical = work_order_id.strip() if work_order_id else None
    legacy = crm_work_order_id.strip() if crm_work_order_id else None
    if canonical and legacy and canonical != legacy:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "field.work_order_id_conflict",
                "message": (
                    "work_order_id and crm_work_order_id must identify the same "
                    "work order"
                ),
            },
        )
    return canonical or legacy
