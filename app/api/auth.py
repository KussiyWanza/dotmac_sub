from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyGenerateResponse,
    ApiKeyRead,
    ApiKeyUpdate,
    MFAMethodCreate,
    MFAMethodRead,
    MFAMethodUpdate,
    SessionCreate,
    SessionRead,
    SessionUpdate,
    UserCredentialCreate,
    UserCredentialRead,
    UserCredentialUpdate,
)
from app.schemas.common import ListResponse
from app.services import auth as auth_service
from app.services.auth_dependencies import require_permission

router = APIRouter()


@router.post(
    "/user-credentials",
    response_model=UserCredentialRead,
    status_code=status.HTTP_201_CREATED,
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:write"))],
)
def create_user_credential(
    payload: UserCredentialCreate, db: Session = Depends(get_db)
):
    return auth_service.user_credentials.create(db, payload)


@router.get(
    "/user-credentials/{credential_id}",
    response_model=UserCredentialRead,
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:read"))],
)
def get_user_credential(credential_id: str, db: Session = Depends(get_db)):
    return auth_service.user_credentials.get(db, credential_id)


@router.get(
    "/user-credentials",
    response_model=ListResponse[UserCredentialRead],
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:read"))],
)
def list_user_credentials(
    subscriber_id: str | None = None,
    provider: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.user_credentials.list_response(
        db,
        subscriber_id=subscriber_id,
        provider=provider,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/user-credentials/{credential_id}",
    response_model=UserCredentialRead,
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:write"))],
)
def update_user_credential(
    credential_id: str, payload: UserCredentialUpdate, db: Session = Depends(get_db)
):
    return auth_service.user_credentials.update(db, credential_id, payload)


@router.delete(
    "/user-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["user-credentials"],
    dependencies=[Depends(require_permission("auth:credential:write"))],
)
def delete_user_credential(credential_id: str, db: Session = Depends(get_db)):
    auth_service.user_credentials.delete(db, credential_id)


@router.post(
    "/mfa-methods",
    response_model=MFAMethodRead,
    status_code=status.HTTP_201_CREATED,
    tags=["mfa-methods"],
)
def create_mfa_method(payload: MFAMethodCreate, db: Session = Depends(get_db)):
    return auth_service.mfa_methods.create(db, payload)


@router.get(
    "/mfa-methods/{method_id}",
    response_model=MFAMethodRead,
    tags=["mfa-methods"],
)
def get_mfa_method(method_id: str, db: Session = Depends(get_db)):
    return auth_service.mfa_methods.get(db, method_id)


@router.get(
    "/mfa-methods",
    response_model=ListResponse[MFAMethodRead],
    tags=["mfa-methods"],
)
def list_mfa_methods(
    subscriber_id: str | None = None,
    method_type: str | None = None,
    is_primary: bool | None = None,
    enabled: bool | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.mfa_methods.list_response(
        db,
        subscriber_id=subscriber_id,
        method_type=method_type,
        is_primary=is_primary,
        enabled=enabled,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/mfa-methods/{method_id}",
    response_model=MFAMethodRead,
    tags=["mfa-methods"],
)
def update_mfa_method(
    method_id: str, payload: MFAMethodUpdate, db: Session = Depends(get_db)
):
    return auth_service.mfa_methods.update(db, method_id, payload)


@router.delete(
    "/mfa-methods/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["mfa-methods"],
)
def delete_mfa_method(method_id: str, db: Session = Depends(get_db)):
    auth_service.mfa_methods.delete(db, method_id)


@router.post(
    "/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)):
    return auth_service.sessions.create(db, payload)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionRead,
    tags=["sessions"],
)
def get_session(session_id: str, db: Session = Depends(get_db)):
    return auth_service.sessions.get(db, session_id)


@router.get(
    "/sessions",
    response_model=ListResponse[SessionRead],
    tags=["sessions"],
)
def list_sessions(
    subscriber_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.sessions.list_response(
        db,
        subscriber_id=subscriber_id,
        status=status,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/sessions/{session_id}",
    response_model=SessionRead,
    tags=["sessions"],
)
def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
):
    return auth_service.sessions.update(db, session_id, payload)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sessions"],
)
def delete_session(session_id: str, db: Session = Depends(get_db)):
    auth_service.sessions.delete(db, session_id)


@router.post(
    "/api-keys",
    response_model=ApiKeyRead,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
)
def create_api_key(payload: ApiKeyCreate, db: Session = Depends(get_db)):
    return auth_service.api_keys.create(db, payload)


@router.post(
    "/api-keys/generate",
    response_model=ApiKeyGenerateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
)
def generate_api_key(
    payload: ApiKeyGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    return auth_service.api_keys.generate_with_rate_limit(db, payload, request)


@router.get(
    "/api-keys/{key_id}",
    response_model=ApiKeyRead,
    tags=["api-keys"],
)
def get_api_key(key_id: str, db: Session = Depends(get_db)):
    return auth_service.api_keys.get(db, key_id)


@router.get(
    "/api-keys",
    response_model=ListResponse[ApiKeyRead],
    tags=["api-keys"],
)
def list_api_keys(
    subscriber_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return auth_service.api_keys.list_response(
        db,
        subscriber_id=subscriber_id,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/api-keys/{key_id}",
    response_model=ApiKeyRead,
    tags=["api-keys"],
)
def update_api_key(key_id: str, payload: ApiKeyUpdate, db: Session = Depends(get_db)):
    return auth_service.api_keys.update(db, key_id, payload)


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api-keys"],
)
def delete_api_key(key_id: str, db: Session = Depends(get_db)):
    auth_service.api_keys.revoke(db, key_id)
