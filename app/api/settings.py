from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.settings import DomainSettingRead, DomainSettingUpdate
from app.services import settings_api as settings_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "/gis", response_model=ListResponse[DomainSettingRead], tags=["settings-gis"]
)
def list_gis_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_gis_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/gis/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-gis"],
)
def upsert_gis_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_gis_setting(db, key, payload)


@router.get(
    "/gis/{key}",
    response_model=DomainSettingRead,
    tags=["settings-gis"],
)
def get_gis_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_gis_setting(db, key)


@router.get(
    "/geocoding",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-geocoding"],
)
def list_geocoding_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_geocoding_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/geocoding/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-geocoding"],
)
def upsert_geocoding_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_geocoding_setting(db, key, payload)


@router.get(
    "/geocoding/{key}",
    response_model=DomainSettingRead,
    tags=["settings-geocoding"],
)
def get_geocoding_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_geocoding_setting(db, key)


@router.get(
    "/radius",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-radius"],
)
def list_radius_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_radius_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/radius/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-radius"],
)
def upsert_radius_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_radius_setting(db, key, payload)


@router.get(
    "/radius/{key}",
    response_model=DomainSettingRead,
    tags=["settings-radius"],
)
def get_radius_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_radius_setting(db, key)


@router.get(
    "/auth",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-auth"],
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def list_auth_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_auth_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/auth/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-auth"],
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def upsert_auth_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_auth_setting(db, key, payload)


@router.get(
    "/auth/{key}",
    response_model=DomainSettingRead,
    tags=["settings-auth"],
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def get_auth_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_auth_setting(db, key)


@router.get(
    "/audit",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-audit"],
)
def list_audit_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_audit_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/audit/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-audit"],
)
def upsert_audit_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_audit_setting(db, key, payload)


@router.get(
    "/audit/{key}",
    response_model=DomainSettingRead,
    tags=["settings-audit"],
)
def get_audit_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_audit_setting(db, key)


@router.get(
    "/imports",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-imports"],
)
def list_imports_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_imports_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/imports/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-imports"],
)
def upsert_imports_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_imports_setting(db, key, payload)


@router.get(
    "/imports/{key}",
    response_model=DomainSettingRead,
    tags=["settings-imports"],
)
def get_imports_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_imports_setting(db, key)


@router.get(
    "/notification",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-notification"],
)
def list_notification_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_notification_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/notification/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-notification"],
)
def upsert_notification_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_notification_setting(db, key, payload)


@router.get(
    "/notification/{key}",
    response_model=DomainSettingRead,
    tags=["settings-notification"],
)
def get_notification_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_notification_setting(db, key)


@router.get(
    "/scheduler",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-scheduler"],
)
def list_scheduler_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_scheduler_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/scheduler/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-scheduler"],
)
def upsert_scheduler_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_scheduler_setting(db, key, payload)


@router.get(
    "/scheduler/{key}",
    response_model=DomainSettingRead,
    tags=["settings-scheduler"],
)
def get_scheduler_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_scheduler_setting(db, key)


@router.get(
    "/billing",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-billing"],
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def list_billing_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_billing_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/billing/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-billing"],
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def upsert_billing_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_billing_setting(db, key, payload)


@router.get(
    "/billing/{key}",
    response_model=DomainSettingRead,
    tags=["settings-billing"],
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def get_billing_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_billing_setting(db, key)


@router.get(
    "/catalog",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-catalog"],
)
def list_catalog_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_catalog_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/catalog/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-catalog"],
)
def upsert_catalog_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_catalog_setting(db, key, payload)


@router.get(
    "/catalog/{key}",
    response_model=DomainSettingRead,
    tags=["settings-catalog"],
)
def get_catalog_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_catalog_setting(db, key)


@router.get(
    "/subscriber",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-subscriber"],
)
def list_subscriber_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_subscriber_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/subscriber/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-subscriber"],
)
def upsert_subscriber_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_subscriber_setting(db, key, payload)


@router.get(
    "/subscriber/{key}",
    response_model=DomainSettingRead,
    tags=["settings-subscriber"],
)
def get_subscriber_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_subscriber_setting(db, key)


@router.get(
    "/usage",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-usage"],
)
def list_usage_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_usage_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/usage/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-usage"],
)
def upsert_usage_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_usage_setting(db, key, payload)


@router.get(
    "/usage/{key}",
    response_model=DomainSettingRead,
    tags=["settings-usage"],
)
def get_usage_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_usage_setting(db, key)


@router.get(
    "/collections",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-collections"],
)
def list_collections_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_collections_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/collections/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-collections"],
)
def upsert_collections_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_collections_setting(db, key, payload)


@router.get(
    "/collections/{key}",
    response_model=DomainSettingRead,
    tags=["settings-collections"],
)
def get_collections_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_collections_setting(db, key)


@router.get(
    "/provisioning",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-provisioning"],
)
def list_provisioning_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_provisioning_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/provisioning/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-provisioning"],
)
def upsert_provisioning_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_provisioning_setting(db, key, payload)


@router.get(
    "/provisioning/{key}",
    response_model=DomainSettingRead,
    tags=["settings-provisioning"],
)
def get_provisioning_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_provisioning_setting(db, key)


@router.get(
    "/network",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-network"],
)
def list_network_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_network_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/network/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-network"],
)
def upsert_network_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_network_setting(db, key, payload)


@router.get(
    "/network/{key}",
    response_model=DomainSettingRead,
    tags=["settings-network"],
)
def get_network_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_network_setting(db, key)


@router.get(
    "/inventory",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-inventory"],
)
def list_inventory_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_inventory_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/inventory/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-inventory"],
)
def upsert_inventory_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_inventory_setting(db, key, payload)


@router.get(
    "/inventory/{key}",
    response_model=DomainSettingRead,
    tags=["settings-inventory"],
)
def get_inventory_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_inventory_setting(db, key)


@router.get(
    "/lifecycle",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-lifecycle"],
)
def list_lifecycle_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_lifecycle_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/lifecycle/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-lifecycle"],
)
def upsert_lifecycle_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_lifecycle_setting(db, key, payload)


@router.get(
    "/lifecycle/{key}",
    response_model=DomainSettingRead,
    tags=["settings-lifecycle"],
)
def get_lifecycle_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_lifecycle_setting(db, key)


@router.get(
    "/comms",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-comms"],
)
def list_comms_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_comms_settings_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.put(
    "/comms/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-comms"],
)
def upsert_comms_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_comms_setting(db, key, payload)


@router.get(
    "/comms/{key}",
    response_model=DomainSettingRead,
    tags=["settings-comms"],
)
def get_comms_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_comms_setting(db, key)


@router.get(
    "/projects",
    response_model=ListResponse[DomainSettingRead],
    tags=["settings-projects"],
)
def list_projects_settings(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return settings_service.list_projects_settings_response(
        db=db,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.put(
    "/projects/{key}",
    response_model=DomainSettingRead,
    status_code=status.HTTP_200_OK,
    tags=["settings-projects"],
)
def upsert_projects_setting(
    key: str, payload: DomainSettingUpdate, db: Session = Depends(get_db)
):
    return settings_service.upsert_projects_setting(db=db, key=key, payload=payload)


@router.get(
    "/projects/{key}",
    response_model=DomainSettingRead,
    tags=["settings-projects"],
)
def get_projects_setting(key: str, db: Session = Depends(get_db)):
    return settings_service.get_projects_setting(db=db, key=key)
