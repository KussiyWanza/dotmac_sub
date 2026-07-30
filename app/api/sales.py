"""Native sales kanban API ported from CRM (``/leads/kanban…``).

Ported from ``dotmac_crm/app/api/sales.py``. Only the kanban endpoints are
public API operations in this capability. Native dashboard reporting is owned
by ``app.services.sales.reports`` and consumed by the server-rendered admin
surface; no CRM reporting fallback or parallel API calculation exists.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.services import sales as sales_service

router = APIRouter(prefix="/leads", tags=["sales"])


class KanbanMoveRequest(BaseModel):
    """Request body for moving a lead on the kanban board."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    to: str  # Target stage ID
    from_: str | None = Field(default=None, alias="from")  # Source stage (optional)
    position: int | None = None  # Position in the column (optional)


@router.get(
    "/kanban",
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_kanban_data(
    pipeline_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Kanban board data for the sales pipeline: columns (stages) and
    records (leads)."""
    return sales_service.Leads.kanban_view(db, pipeline_id)


@router.post(
    "/kanban/move",
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def move_kanban_card(
    request: KanbanMoveRequest,
    db: Session = Depends(get_db),
):
    """Move a lead to a different stage on the kanban board.

    Auto-updates probability from the target stage's default if unset.
    """
    return sales_service.Leads.update_stage(db, request.id, request.to)
