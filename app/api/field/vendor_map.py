from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldMapAssetNearbyResponse
from app.services.field import vendor_capabilities
from app.services.field.map_assets import field_map_assets
from app.services.field.vendor_auth import require_field_vendor_token

router = APIRouter(prefix="/vendor/map-assets", tags=["field-vendor-map"])


def _require_project_read(
    context: dict = Depends(require_field_vendor_token),
) -> dict:
    """Plant lookup is project work, so it needs the project-read capability.

    This route resolved the vendor context and then ignored it, so any vendor
    member reached plant regardless of role. The native-vendor link is
    deliberately not required here: unlike the portal plane this read touches
    no native vendor record, and demanding the bridge would refuse legitimate
    unbridged crews.

    Scoping remains proximity-only — a vendor sees active plant near the point
    they ask about, not only plant on their own projects. Narrowing that needs
    an assignment-to-plant relationship that does not exist yet.
    """

    if not vendor_capabilities.has_capability(
        context, vendor_capabilities.PROJECT_READ
    ):
        raise HTTPException(
            status_code=403,
            detail=vendor_capabilities.refusal_message(
                vendor_capabilities.PROJECT_READ
            ),
        )
    return context


def _parse_types(types: str | None) -> list[str] | None:
    if not types:
        return None
    return [item.strip() for item in types.split(",") if item.strip()]


@router.get("/nearby", response_model=FieldMapAssetNearbyResponse)
def get_vendor_nearby_map_assets(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(default=1000.0, gt=0, le=20000),
    types: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _vendor: dict = Depends(_require_project_read),
    db: Session = Depends(get_db),
):
    items = field_map_assets.nearby(
        db,
        latitude=lat,
        longitude=lng,
        radius_m=radius_m,
        asset_types=_parse_types(types),
        limit=limit,
    )
    return {
        "items": items,
        "count": len(items),
        "latitude": lat,
        "longitude": lng,
        "radius_m": radius_m,
        "server_time": datetime.now(UTC),
    }
