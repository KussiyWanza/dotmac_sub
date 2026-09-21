"""Thin ERP HR adapter for the canonical staff-provisioning owner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.system_user import SystemUser
from app.services import (
    nextcloud_talk_staff,
    service_team_lifecycle,
    staff_provisioning,
)
from app.services.auth_dependencies import require_permission
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/staff-accounts", tags=["staff-sync"])

RoleName = Annotated[str, Field(min_length=1, max_length=120)]
ERP_DEPARTMENT_SYNC_SCOPE = "operations:service_team:membership"
DEFAULT_ERP_ACCOUNT_SCOPE = "default"
IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


class StaffAccountCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="staff", min_length=1, max_length=80)
    roles: list[RoleName] | None = Field(default=None, min_length=1, max_length=20)
    send_invite: bool = True
    existing_account_policy: staff_provisioning.ExistingStaffAccountPolicy = (
        staff_provisioning.ExistingStaffAccountPolicy.reconcile
    )


class StaffAccountRolesUpdate(BaseModel):
    roles: list[RoleName] = Field(min_length=1, max_length=20)


class ErpDepartmentReference(BaseModel):
    department_id: str = Field(min_length=1, max_length=200)
    department_code: str | None = Field(default=None, max_length=80)
    department_name: str | None = Field(default=None, max_length=200)


class StaffAccountErpDepartmentUpdate(BaseModel):
    erp_employee_id: str = Field(min_length=1, max_length=200)
    employee_code: str | None = Field(default=None, max_length=80)
    erp_organization_id: UUID | None = None
    account_scope: str | None = Field(default=None, max_length=120)
    department: ErpDepartmentReference | None = None


class StaffAccountRead(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    is_active: bool
    roles: list[str] = Field(default_factory=list)
    created: bool = False
    changed: bool = False
    invite_requested: bool = False


class StaffAccountErpDepartmentRead(BaseModel):
    user_id: UUID
    erp_employee_id: str
    account_scope: str
    service_team_id: UUID | None
    previous_service_team_id: UUID | None
    member_id: UUID | None
    operation: str
    changed: bool
    replayed: bool


class StaffAccountNextcloudTalkUpdate(BaseModel):
    nextcloud_user_id: str = Field(min_length=1, max_length=255)


class StaffAccountNextcloudTalkRead(BaseModel):
    user_id: UUID
    integration_installation_id: UUID
    nextcloud_user_id: str
    is_active: bool


class StaffAccountNextcloudTalkDisableRead(BaseModel):
    user_id: UUID
    disabled_mappings: int


def _actor(auth: dict) -> str:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return f"{actor_type}:{principal_id}"


def _context(
    auth: dict,
    *,
    reason: str,
    idempotency_key: str,
    scope: str = staff_provisioning.STAFF_ASSIGN_SCOPE,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(auth),
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _domain_error(exc: DomainError) -> HTTPException:
    if (
        exc.code.endswith(".unknown_roles")
        or exc.code.endswith(".invalid_command")
        or exc.code.endswith(".invalid_mapping")
        or exc.code
        in {
            "service_team_invalid",
            "service_team_erp_department_unmapped",
            "service_team_staff_identity_unbound",
            "service_team_staff_identity_invalid",
        }
    ):
        status_code = 422
    elif exc.code.endswith(".staff_account_not_found") or exc.code in {
        "service_team_not_found",
        "service_team_staff_not_found",
    }:
        status_code = 404
    elif (
        exc.code.endswith(".identity_conflict")
        or exc.code.endswith(".active_caller_transaction")
        or exc.code.endswith(".last_admin_required")
        or exc.code == "service_team_erp_employee_identity_conflict"
    ):
        status_code = 409
    elif exc.code.endswith(".binding_unavailable"):
        status_code = 503
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _from_outcome(
    outcome: staff_provisioning.StaffAccountOutcome,
) -> StaffAccountRead:
    return StaffAccountRead(
        id=outcome.user_id,
        email=outcome.email,
        display_name=outcome.display_name,
        is_active=outcome.is_active,
        roles=list(outcome.role_names),
        created=outcome.created,
        changed=outcome.changed,
        invite_requested=outcome.invite_requested,
    )


def _from_user(db: Session, user: SystemUser) -> StaffAccountRead:
    return StaffAccountRead(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        roles=staff_provisioning.get_role_names(db, user),
    )


def _erp_account_scope(payload: StaffAccountErpDepartmentUpdate) -> str:
    account_scope = str(payload.account_scope or "").strip()
    if account_scope:
        return account_scope
    if payload.erp_organization_id is not None:
        return str(payload.erp_organization_id)
    return DEFAULT_ERP_ACCOUNT_SCOPE


@router.post("", response_model=StaffAccountRead)
def create_staff_account(
    payload: StaffAccountCreate,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    """Create or reconcile a staff account by canonical email."""

    normalized_email = str(payload.email).strip().lower()
    desired_roles = tuple(payload.roles or [payload.role])
    try:
        result = staff_provisioning.provision_staff_account(
            db,
            staff_provisioning.ProvisionStaffAccountCommand(
                context=_context(
                    auth,
                    reason="ERP HR staff account reconciliation",
                    idempotency_key=(
                        idempotency_key or f"staff-account:{normalized_email}"
                    ),
                ),
                email=normalized_email,
                first_name=payload.first_name,
                last_name=payload.last_name,
                role_names=desired_roles,
                send_invite=payload.send_invite,
                existing_account_policy=payload.existing_account_policy,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return _from_outcome(result)


@router.put(
    "/{user_id}/nextcloud-talk",
    response_model=StaffAccountNextcloudTalkRead,
)
def set_staff_nextcloud_talk_mapping(
    user_id: str,
    payload: StaffAccountNextcloudTalkUpdate,
    auth: dict = Depends(require_permission(nextcloud_talk_staff.COMMAND_SCOPE)),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountNextcloudTalkRead:
    """Bind one active ERP staff account to its provisioned Nextcloud identity."""

    try:
        normalized_user_id = coerce_uuid(user_id)
        nextcloud_user_id = nextcloud_talk_staff.NextcloudUserId.parse(
            payload.nextcloud_user_id
        )
        outcome = nextcloud_talk_staff.execute_set_default_staff_account_mapping(
            db,
            nextcloud_talk_staff.SetDefaultStaffAccountMappingCommand(
                context=_context(
                    auth,
                    reason="ERP workforce Nextcloud Talk mapping reconciliation",
                    idempotency_key=(
                        idempotency_key
                        or f"staff-talk:{normalized_user_id}:{nextcloud_user_id.normalized}"
                    ),
                    scope=nextcloud_talk_staff.COMMAND_SCOPE,
                ),
                system_user_id=normalized_user_id,
                nextcloud_user_id=nextcloud_user_id,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return StaffAccountNextcloudTalkRead(
        user_id=outcome.system_user_id,
        integration_installation_id=outcome.integration_installation_id,
        nextcloud_user_id=outcome.nextcloud_user_id.value,
        is_active=outcome.is_active,
    )


@router.post(
    "/{user_id}/nextcloud-talk/disable",
    response_model=StaffAccountNextcloudTalkDisableRead,
)
def disable_staff_nextcloud_talk_mapping(
    user_id: str,
    auth: dict = Depends(require_permission(nextcloud_talk_staff.COMMAND_SCOPE)),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountNextcloudTalkDisableRead:
    """Disable all Talk delivery mappings for an ERP-deactivated staff account."""

    try:
        normalized_user_id = coerce_uuid(user_id)
        outcome = nextcloud_talk_staff.execute_disable_all_staff_account_mappings(
            db,
            nextcloud_talk_staff.DisableAllStaffAccountMappingsCommand(
                context=_context(
                    auth,
                    reason="ERP workforce Nextcloud Talk mapping deactivation",
                    idempotency_key=(
                        idempotency_key or f"staff-talk-disable:{normalized_user_id}"
                    ),
                    scope=nextcloud_talk_staff.COMMAND_SCOPE,
                ),
                system_user_id=normalized_user_id,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return StaffAccountNextcloudTalkDisableRead(
        user_id=outcome.system_user_id,
        disabled_mappings=outcome.disabled_mappings,
    )


@router.put("/{user_id}/erp-department", response_model=StaffAccountErpDepartmentRead)
def sync_staff_erp_department(
    user_id: str,
    payload: StaffAccountErpDepartmentUpdate,
    auth: dict = Depends(require_permission(ERP_DEPARTMENT_SYNC_SCOPE)),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountErpDepartmentRead:
    """Assign, transfer, or remove the ERP-managed service-team membership."""

    try:
        normalized_user_id = coerce_uuid(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    account_scope = _erp_account_scope(payload)
    department = (
        service_team_lifecycle.ErpDepartmentMembershipDepartment(
            department_id=payload.department.department_id,
            department_code=payload.department.department_code,
            department_name=payload.department.department_name,
        )
        if payload.department is not None
        else None
    )
    department_key = payload.department.department_id if payload.department else "none"
    try:
        result = service_team_lifecycle.sync_erp_department_membership(
            db,
            service_team_lifecycle.SyncErpDepartmentMembership(
                context=_context(
                    auth,
                    reason="ERP HR department/service-team membership reconciliation",
                    idempotency_key=(
                        idempotency_key
                        or (
                            f"erp-department:{normalized_user_id}:"
                            f"{payload.erp_employee_id}:{account_scope}:"
                            f"{department_key}"
                        )
                    ),
                    scope=ERP_DEPARTMENT_SYNC_SCOPE,
                ),
                system_user_id=normalized_user_id,
                account_scope=account_scope,
                erp_employee_id=payload.erp_employee_id,
                employee_code=payload.employee_code,
                department=department,
                observed_at=datetime.now(UTC),
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return StaffAccountErpDepartmentRead(
        user_id=result.system_user_id,
        erp_employee_id=result.erp_employee_id,
        account_scope=result.account_scope,
        service_team_id=result.team_id,
        previous_service_team_id=result.previous_team_id,
        member_id=result.member_id,
        operation=result.operation,
        changed=not result.replayed,
        replayed=result.replayed,
    )


@router.get("", response_model=StaffAccountRead)
def get_staff_account(
    email: EmailStr = Query(...),
    _auth: dict = Depends(require_permission("rbac:roles:read")),
    db: Session = Depends(get_db),
) -> StaffAccountRead:
    """Look up a staff account for an ERP reconcile sweep."""

    user = staff_provisioning.find_by_email(db, str(email))
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _from_user(db, user)


@router.put("/{user_id}/roles", response_model=StaffAccountRead)
def update_staff_account_roles(
    user_id: str,
    payload: StaffAccountRolesUpdate,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    try:
        normalized_user_id = coerce_uuid(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    canonical_roles = tuple(dict.fromkeys(role.strip() for role in payload.roles))
    try:
        result = staff_provisioning.sync_staff_account_roles(
            db,
            staff_provisioning.SyncStaffRolesCommand(
                context=_context(
                    auth,
                    reason="ERP HR staff role reconciliation",
                    idempotency_key=(
                        idempotency_key
                        or f"staff-roles:{normalized_user_id}:{','.join(sorted(canonical_roles))}"
                    ),
                ),
                user_id=normalized_user_id,
                role_names=canonical_roles,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return _from_outcome(result)


def _set_active(
    *,
    user_id: str,
    is_active: bool,
    auth: dict,
    db: Session,
    idempotency_key: str | None,
) -> StaffAccountRead:
    try:
        normalized_user_id = coerce_uuid(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    state = "active" if is_active else "inactive"
    try:
        result = staff_provisioning.set_staff_account_active(
            db,
            staff_provisioning.SetStaffAccountActiveCommand(
                context=_context(
                    auth,
                    reason=f"ERP HR staff account {state} reconciliation",
                    idempotency_key=(
                        idempotency_key or f"staff-state:{normalized_user_id}:{state}"
                    ),
                ),
                user_id=normalized_user_id,
                is_active=is_active,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return _from_outcome(result)


@router.post("/{user_id}/activate", response_model=StaffAccountRead)
def activate_staff_account(
    user_id: str,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    return _set_active(
        user_id=user_id,
        is_active=True,
        auth=auth,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/{user_id}/deactivate", response_model=StaffAccountRead)
def deactivate_staff_account(
    user_id: str,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    """Disable a staff account, credentials, and live sessions atomically."""

    return _set_active(
        user_id=user_id,
        is_active=False,
        auth=auth,
        db=db,
        idempotency_key=idempotency_key,
    )
