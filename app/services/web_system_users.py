"""Service helpers for admin system user listing/statistics."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.auth import MFAMethod, UserCredential
from app.models.rbac import Permission, Role, SystemUserPermission, SystemUserRole
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.schemas.status_presentation import StatusTone
from app.services.dynamic_filters import (
    DEFAULT_OPERATORS_BY_TYPE,
    NULL_TOKENS,
    OPERATOR_LABELS,
    FilterCondition,
    FilterFieldSpec,
    FilterQuery,
    FilterValidationError,
    _coerce_list,
    _coerce_scalar,
    _parse_bool,
    build_filter_expression,
    build_sort_clause,
    parse_filter_payload,
)
from app.services.ui_contracts import Kpi, StateValue

logger = logging.getLogger(__name__)

USER_TYPE_OPTIONS = [("system_user", "System User")]
USER_LIST_TYPE_OPTIONS = [
    ("system_user", "System User"),
    ("reseller", "Reseller User"),
]
USER_TYPE_LABELS = {key: label for key, label in USER_LIST_TYPE_OPTIONS}
USER_DOCTYPE = "User"


def customer_experience_user_options(db: Session) -> list[dict[str, str]]:
    """Return active users granted the canonical Customer Experience role."""

    normalized_role = func.replace(
        func.replace(func.lower(func.trim(Role.name)), "_", " "), "-", " "
    )
    rows = (
        db.query(SystemUser)
        .join(SystemUserRole, SystemUserRole.system_user_id == SystemUser.id)
        .join(Role, Role.id == SystemUserRole.role_id)
        .filter(
            SystemUser.is_active.is_(True),
            Role.is_active.is_(True),
            normalized_role == "customer experience",
        )
        .distinct()
        .order_by(SystemUser.first_name.asc(), SystemUser.last_name.asc())
        .all()
    )
    return [
        {
            "id": str(user.id),
            "name": (
                (user.display_name or "").strip()
                or f"{user.first_name} {user.last_name}".strip()
                or user.email
            ),
            "email": user.email or "",
        }
        for user in rows
    ]


def _labels_by_uuid(db: Session, model, label_attr: str, ids: set[str]) -> list[str]:
    values: list[UUID] = []
    for item in ids:
        try:
            values.append(UUID(str(item)))
        except (TypeError, ValueError):
            continue
    if not values:
        return []
    rows = db.execute(select(model).where(model.id.in_(values))).scalars().all()
    labels = [str(getattr(row, label_attr, row.id)) for row in rows]
    return sorted(labels)


def build_user_audit_snapshot(db: Session, user: SystemUser) -> dict[str, object]:
    role_ids = {
        str(role_id)
        for role_id in db.execute(
            select(SystemUserRole.role_id).where(
                SystemUserRole.system_user_id == user.id
            )
        ).scalars()
    }
    permission_ids = {
        str(permission_id)
        for permission_id in db.execute(
            select(SystemUserPermission.permission_id).where(
                SystemUserPermission.system_user_id == user.id
            )
        ).scalars()
    }
    return {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "display_name": user.display_name,
        "email": user.email,
        "phone": user.phone,
        "is_active": user.is_active,
        "roles": _labels_by_uuid(db, Role, "name", role_ids),
        "direct_permissions": _labels_by_uuid(db, Permission, "key", permission_ids),
    }


def _pending_credential_expression():
    active_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.system_user_id == SystemUser.id)
        .where(UserCredential.is_active.is_(True))
    )
    pending_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.system_user_id == SystemUser.id)
        .where(UserCredential.is_active.is_(True))
        .where(UserCredential.must_change_password.is_(True))
    )
    return or_(~active_credential, pending_credential)


def _pending_reseller_credential_expression():
    active_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.subscriber_id == Subscriber.id)
        .where(UserCredential.is_active.is_(True))
    )
    pending_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.subscriber_id == Subscriber.id)
        .where(UserCredential.is_active.is_(True))
        .where(UserCredential.must_change_password.is_(True))
    )
    return or_(~active_credential, pending_credential)


def _parse_uuid_value(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FilterValidationError("Expected a valid UUID value") from exc


def _parse_uuid_list(value: object) -> list[UUID]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        raise FilterValidationError("Expected an array value")
    if not items:
        raise FilterValidationError("Array filter value cannot be empty")
    return [_parse_uuid_value(item) for item in items]


def _role_filter_expression(operator: str, value: object):
    if operator == "is":
        token = str(value).strip().lower() if value is not None else ""
        if token in {"", "null", "none", "nil"}:
            return ~exists(
                select(SystemUserRole.id).where(
                    SystemUserRole.system_user_id == SystemUser.id
                )
            )
        raise FilterValidationError("Role field supports only 'is null'")

    if operator == "is not":
        token = str(value).strip().lower() if value is not None else ""
        if token in {"", "null", "none", "nil"}:
            return exists(
                select(SystemUserRole.id).where(
                    SystemUserRole.system_user_id == SystemUser.id
                )
            )
        raise FilterValidationError("Role field supports only 'is not null'")

    if operator == "=":
        role_id = _parse_uuid_value(value)
        return exists(
            select(SystemUserRole.id)
            .where(SystemUserRole.system_user_id == SystemUser.id)
            .where(SystemUserRole.role_id == role_id)
        )

    if operator == "!=":
        role_id = _parse_uuid_value(value)
        return ~exists(
            select(SystemUserRole.id)
            .where(SystemUserRole.system_user_id == SystemUser.id)
            .where(SystemUserRole.role_id == role_id)
        )

    if operator == "in":
        role_ids = _parse_uuid_list(value)
        return exists(
            select(SystemUserRole.id)
            .where(SystemUserRole.system_user_id == SystemUser.id)
            .where(SystemUserRole.role_id.in_(role_ids))
        )

    if operator == "not in":
        role_ids = _parse_uuid_list(value)
        return ~exists(
            select(SystemUserRole.id)
            .where(SystemUserRole.system_user_id == SystemUser.id)
            .where(SystemUserRole.role_id.in_(role_ids))
        )

    raise FilterValidationError(f"Operator '{operator}' is not allowed for role_id")


def _status_filter_expression(operator: str, value: object):
    status_value = str(value).strip().lower()
    if status_value not in {"active", "inactive", "pending"}:
        raise FilterValidationError("Status must be one of: active, inactive, pending")

    if status_value == "active":
        expr = SystemUser.is_active.is_(True)
    elif status_value == "inactive":
        expr = SystemUser.is_active.is_(False)
    else:
        expr = _pending_credential_expression()

    if operator == "=":
        return expr
    if operator == "!=":
        return ~expr
    raise FilterValidationError("Status field supports only '=' and '!='")


def _mfa_filter_expression(operator: str, value: object):
    mfa_exists = exists(
        select(MFAMethod.id)
        .where(MFAMethod.system_user_id == SystemUser.id)
        .where(MFAMethod.enabled.is_(True))
        .where(MFAMethod.is_active.is_(True))
    )

    value_text = str(value).strip().lower() if value is not None else ""
    if value_text in {"true", "1", "yes", "on"}:
        target = mfa_exists
    elif value_text in {"false", "0", "no", "off"}:
        target = ~mfa_exists
    else:
        raise FilterValidationError("mfa_enabled expects a boolean value")

    if operator in {"=", "is"}:
        return target
    if operator in {"!=", "is not"}:
        return ~target
    raise FilterValidationError("mfa_enabled supports '=', '!=', 'is', and 'is not'")


def _last_login_expression():
    return (
        select(func.max(UserCredential.last_login_at))
        .where(UserCredential.system_user_id == SystemUser.id)
        .scalar_subquery()
    )


def _normalize_field_value(value: Any) -> Any:
    if isinstance(value, UserType):
        return value.value
    return value


def _row_matches_condition(row: dict[str, Any], condition: FilterCondition) -> bool:
    spec = USER_FILTER_SPECS.get(condition.field)
    if spec is None:
        return False

    field = condition.field
    operator = condition.operator
    field_type = spec.field_type

    if field == "role_id":
        role_ids = [item["id"] for item in row.get("roles", [])]
        if operator == "is":
            token = (
                str(condition.value).strip().lower()
                if condition.value is not None
                else ""
            )
            return token in NULL_TOKENS and not role_ids
        if operator == "is not":
            token = (
                str(condition.value).strip().lower()
                if condition.value is not None
                else ""
            )
            return token in NULL_TOKENS and bool(role_ids)
        if operator in {"=", "!="}:
            role_id = str(_parse_uuid_value(condition.value))
            matched = role_id in role_ids
            return matched if operator == "=" else not matched
        if operator in {"in", "not in"}:
            wanted = {str(item) for item in _parse_uuid_list(condition.value)}
            matched = any(role_id in wanted for role_id in role_ids)
            return matched if operator == "in" else not matched
        return False

    if field == "status":
        current_value = (
            "pending"
            if row.get("is_pending")
            else ("active" if row.get("is_active") else "inactive")
        )
        expected = str(condition.value).strip().lower()
        if operator == "=":
            return current_value == expected
        if operator == "!=":
            return current_value != expected
        return False

    current_value = _normalize_field_value(row.get(field))

    if operator in {"like", "not like"}:
        pattern = str(_coerce_scalar(condition.value, "text")).lower()
        haystack = str(current_value or "").lower()
        matched = pattern in haystack
        return matched if operator == "like" else not matched

    if operator in {"in", "not in"}:
        expected_values = [
            _normalize_field_value(item)
            for item in _coerce_list(condition.value, field_type)
        ]
        matched = current_value in expected_values
        return matched if operator == "in" else not matched

    if operator == "between":
        if not isinstance(condition.value, (list, tuple)) or len(condition.value) != 2:
            raise FilterValidationError("Between expects [from, to]")
        start = _coerce_scalar(condition.value[0], field_type)
        end = _coerce_scalar(condition.value[1], field_type)
        return current_value is not None and start <= current_value <= end

    if operator in {"is", "is not"}:
        token = (
            str(condition.value).strip().lower()
            if condition.value is not None
            else None
        )
        if token in NULL_TOKENS:
            matched = current_value is None
            return matched if operator == "is" else not matched
        if field_type == "boolean":
            expected = _parse_bool(condition.value)
            matched = bool(current_value) is expected
            return matched if operator == "is" else not matched
        return False

    expected = _normalize_field_value(_coerce_scalar(condition.value, field_type))
    if operator == "=":
        return current_value == expected
    if operator == "!=":
        return current_value != expected
    if operator == ">":
        return current_value is not None and current_value > expected
    if operator == "<":
        return current_value is not None and current_value < expected
    if operator == ">=":
        return current_value is not None and current_value >= expected
    if operator == "<=":
        return current_value is not None and current_value <= expected
    return False


def _row_matches_filter_query(row: dict[str, Any], filter_query: FilterQuery) -> bool:
    and_match = all(
        _row_matches_condition(row, condition) for condition in filter_query.and_filters
    )
    or_match = True
    if filter_query.or_filters:
        or_match = any(
            _row_matches_condition(row, condition)
            for condition in filter_query.or_filters
        )
    group_match = all(
        any(_row_matches_condition(row, condition) for condition in group)
        for group in filter_query.or_groups
    )
    return and_match and or_match and group_match


def _sort_field_name(order_by: str | None) -> str:
    return (order_by or "last_name").strip()


def _sort_direction(order_dir: str | None) -> str:
    return (order_dir or "asc").strip().lower()


def _sort_value(value: Any) -> Any:
    normalized = _normalize_field_value(value)
    if isinstance(normalized, str):
        return normalized.lower()
    if isinstance(normalized, datetime):
        return normalized
    return normalized


def _user_sort_key(row: dict[str, Any], sort_field: str) -> tuple[Any, Any, Any]:
    primary = _sort_value(row.get(sort_field))
    secondary = _sort_value(row.get("last_name"))
    tertiary = _sort_value(row.get("first_name"))
    return (primary is None, primary, secondary, tertiary)


USER_FILTER_SPECS: dict[str, FilterFieldSpec] = {
    "first_name": FilterFieldSpec(
        field="first_name", expression=SystemUser.first_name, field_type="text"
    ),
    "last_name": FilterFieldSpec(
        field="last_name", expression=SystemUser.last_name, field_type="text"
    ),
    "display_name": FilterFieldSpec(
        field="display_name", expression=SystemUser.display_name, field_type="text"
    ),
    "email": FilterFieldSpec(
        field="email", expression=SystemUser.email, field_type="text"
    ),
    "user_type": FilterFieldSpec(
        field="user_type",
        expression=SystemUser.user_type,
        field_type="select",
        options={item[0] for item in USER_LIST_TYPE_OPTIONS},
    ),
    "is_active": FilterFieldSpec(
        field="is_active", expression=SystemUser.is_active, field_type="boolean"
    ),
    "status": FilterFieldSpec(
        field="status",
        field_type="select",
        options={"active", "inactive", "pending"},
        operators={"=", "!="},
        builder=_status_filter_expression,
    ),
    "role_id": FilterFieldSpec(
        field="role_id",
        field_type="uuid",
        operators={"=", "!=", "in", "not in", "is", "is not"},
        builder=_role_filter_expression,
    ),
    "mfa_enabled": FilterFieldSpec(
        field="mfa_enabled",
        field_type="boolean",
        operators={"=", "!=", "is", "is not"},
        builder=_mfa_filter_expression,
    ),
    "created_at": FilterFieldSpec(
        field="created_at", expression=SystemUser.created_at, field_type="datetime"
    ),
    "updated_at": FilterFieldSpec(
        field="updated_at", expression=SystemUser.updated_at, field_type="datetime"
    ),
    "last_login": FilterFieldSpec(
        field="last_login", expression=_last_login_expression(), field_type="datetime"
    ),
}


USER_SORT_FIELDS = {
    "first_name": SystemUser.first_name,
    "last_name": SystemUser.last_name,
    "email": SystemUser.email,
    "created_at": SystemUser.created_at,
    "updated_at": SystemUser.updated_at,
}


def normalize_user_type(value: str | None) -> UserType:
    if value in USER_TYPE_LABELS:
        return UserType(value)
    return UserType.system_user


def user_type_label(value: UserType | str | None) -> str:
    if isinstance(value, UserType):
        key = value.value
    elif isinstance(value, str):
        key = value
    else:
        key = UserType.system_user.value
    return USER_TYPE_LABELS.get(key, "System User")


def _admin_role_id(db: Session) -> UUID | None:
    return db.scalar(
        select(Role.id)
        .where(func.lower(Role.name) == "admin")
        .where(Role.is_active.is_(True))
        .limit(1)
    )


def get_user_stats(db: Session) -> dict[str, int]:
    total = (db.scalar(select(func.count()).select_from(SystemUser)) or 0) + (
        db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(Subscriber.user_type == UserType.reseller)
        )
        or 0
    )
    active = (
        db.scalar(
            select(func.count())
            .select_from(SystemUser)
            .where(SystemUser.is_active.is_(True))
        )
        or 0
    ) + (
        db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(Subscriber.user_type == UserType.reseller)
            .where(Subscriber.is_active.is_(True))
        )
        or 0
    )

    admin_role_id = _admin_role_id(db)
    admins = 0
    if admin_role_id:
        admins = (
            db.scalar(
                select(func.count(func.distinct(SystemUserRole.system_user_id)))
                .select_from(SystemUserRole)
                .where(SystemUserRole.role_id == admin_role_id)
            )
            or 0
        )

    pending = (
        db.scalar(
            select(func.count())
            .select_from(SystemUser)
            .where(_pending_credential_expression())
        )
        or 0
    ) + (
        db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(Subscriber.user_type == UserType.reseller)
            .where(_pending_reseller_credential_expression())
        )
        or 0
    )

    return {"total": total, "active": active, "admins": admins, "pending": pending}


def _users_cohort_url(*, status: str | None = None, role_id: UUID | None = None) -> str:
    """Drill-down to the user list filtered to exactly the cohort a KPI counts.

    The list route accepts ``status`` (active/inactive/pending) and ``role`` as
    real filters, so a headline total and the rows it summarises never diverge
    (KPI-parity rule).
    """
    params = {"status": status, "role": str(role_id) if role_id else None}
    query = urlencode({key: value for key, value in params.items() if value})
    return "/admin/system/users" + (f"?{query}" if query else "")


def build_user_kpis(db: Session) -> dict[str, Kpi]:
    """Headline user tiles as KPI contracts drilling into their exact cohort.

    Counts stay owned by :func:`get_user_stats` (also read by the system
    overview); this is a thin presentation projection over them. The admins
    cohort links to the admin role filter only when an admin role exists — its
    count is zero otherwise, so the base list is the honest fallback.
    """
    counts = get_user_stats(db)
    admin_role_id = _admin_role_id(db)
    return {
        "total": Kpi(
            label="Total Users",
            value=StateValue.present(counts["total"]),
            cohort_url=_users_cohort_url(),
        ),
        "active": Kpi(
            label="Active",
            value=StateValue.present(counts["active"]),
            cohort_url=_users_cohort_url(status="active"),
            tone=StatusTone.positive,
        ),
        "admins": Kpi(
            label="Admins",
            value=StateValue.present(counts["admins"]),
            cohort_url=_users_cohort_url(role_id=admin_role_id),
            tone=StatusTone.info,
        ),
        "pending": Kpi(
            label="Pending Invites",
            value=StateValue.present(counts["pending"]),
            cohort_url=_users_cohort_url(status="pending"),
            tone=StatusTone.warning,
        ),
    }


def _legacy_filters(
    *, search: str | None, role_id: str | None, status: str | None
) -> FilterQuery:
    and_rows: list[FilterCondition] = []
    or_rows: list[FilterCondition] = []

    if search and search.strip():
        query = search.strip()
        or_rows.extend(
            [
                FilterCondition(USER_DOCTYPE, "first_name", "like", query),
                FilterCondition(USER_DOCTYPE, "last_name", "like", query),
                FilterCondition(USER_DOCTYPE, "email", "like", query),
                FilterCondition(USER_DOCTYPE, "display_name", "like", query),
            ]
        )

    if role_id:
        and_rows.append(FilterCondition(USER_DOCTYPE, "role_id", "=", role_id))

    if status:
        and_rows.append(FilterCondition(USER_DOCTYPE, "status", "=", status))

    return FilterQuery(and_filters=and_rows, or_filters=or_rows)


def _merge_filter_queries(*queries: FilterQuery) -> FilterQuery:
    and_filters: list[FilterCondition] = []
    or_filters: list[FilterCondition] = []
    or_groups: list[list[FilterCondition]] = []
    for query in queries:
        and_filters.extend(query.and_filters)
        or_filters.extend(query.or_filters)
        or_groups.extend(query.or_groups)
    return FilterQuery(
        and_filters=and_filters, or_filters=or_filters, or_groups=or_groups
    )


def _serialize_filter_schema(db: Session) -> list[dict[str, object]]:
    roles = list_active_roles(db)
    role_options = [{"value": str(role.id), "label": role.name} for role in roles]

    options_map: dict[str, list[dict[str, str]]] = {
        "user_type": [
            {"value": value, "label": label} for value, label in USER_LIST_TYPE_OPTIONS
        ],
        "status": [
            {"value": "active", "label": "Active"},
            {"value": "inactive", "label": "Inactive"},
            {"value": "pending", "label": "Pending"},
        ],
        "is_active": [
            {"value": "true", "label": "True"},
            {"value": "false", "label": "False"},
        ],
        "mfa_enabled": [
            {"value": "true", "label": "Enabled"},
            {"value": "false", "label": "Disabled"},
        ],
        "role_id": role_options,
    }

    labels = {
        "first_name": "First Name",
        "last_name": "Last Name",
        "display_name": "Display Name",
        "email": "Email",
        "user_type": "User Type",
        "is_active": "Is Active",
        "status": "Status",
        "role_id": "Role",
        "mfa_enabled": "MFA Enabled",
        "created_at": "Created At",
        "updated_at": "Updated At",
        "last_login": "Last Login",
    }

    schema: list[dict[str, object]] = []
    for field_name, spec in USER_FILTER_SPECS.items():
        operators = (
            sorted(spec.operators)
            if spec.operators is not None
            else sorted(DEFAULT_OPERATORS_BY_TYPE.get(spec.field_type, {"="}))
        )
        schema.append(
            {
                "field": field_name,
                "label": labels.get(field_name, field_name.replace("_", " ").title()),
                "type": spec.field_type,
                "operators": [
                    {
                        "value": operator,
                        "label": OPERATOR_LABELS.get(operator, operator),
                    }
                    for operator in operators
                    if operator in OPERATOR_LABELS
                ],
                "options": options_map.get(field_name, []),
            }
        )

    return schema


def list_users(
    db: Session,
    *,
    search: str | None,
    role_id: str | None,
    status: str | None,
    filters: str | None,
    order_by: str | None,
    order_dir: str | None,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    stmt = select(SystemUser)

    try:
        dynamic_query = parse_filter_payload(filters, default_doctype=USER_DOCTYPE)
        legacy_query = _legacy_filters(search=search, role_id=role_id, status=status)
        merged_query = _merge_filter_queries(dynamic_query, legacy_query)
        where_clause = build_filter_expression(
            merged_query,
            doctype=USER_DOCTYPE,
            field_specs=USER_FILTER_SPECS,
        )
        if where_clause is not None:
            stmt = stmt.where(where_clause)

        _ = build_sort_clause(
            order_by=order_by,
            order_dir=order_dir,
            allowed_sort_fields=USER_SORT_FIELDS,
            default_field="last_name",
            default_dir="asc",
        )
    except FilterValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sort_field = _sort_field_name(order_by)
    reverse = _sort_direction(order_dir) == "desc"

    system_users_rows = db.execute(stmt).scalars().all()
    system_user_ids = [row.id for row in system_users_rows]

    reseller_rows = (
        db.execute(select(Subscriber).where(Subscriber.user_type == UserType.reseller))
        .scalars()
        .all()
    )
    reseller_ids = [row.id for row in reseller_rows]

    credentials = (
        db.execute(
            select(UserCredential).where(
                or_(
                    UserCredential.system_user_id.in_(system_user_ids or [UUID(int=0)]),
                    UserCredential.subscriber_id.in_(reseller_ids or [UUID(int=0)]),
                )
            )
        )
        .scalars()
        .all()
    )

    credential_info: dict[Any, dict[str, Any]] = {}
    for credential in credentials:
        principal_id = credential.system_user_id or credential.subscriber_id
        if principal_id is None:
            continue
        info = credential_info.setdefault(
            principal_id,
            {"last_login": None, "has_active": False, "must_change_password": False},  # nosec
        )
        if credential.is_active:
            info["has_active"] = True
            if credential.must_change_password:
                info["must_change_password"] = True
        if credential.last_login_at and (
            info["last_login"] is None or credential.last_login_at > info["last_login"]
        ):
            info["last_login"] = credential.last_login_at

    mfa_enabled_system = set(
        db.execute(
            select(MFAMethod.system_user_id)
            .where(MFAMethod.system_user_id.in_(system_user_ids or [UUID(int=0)]))
            .where(MFAMethod.enabled.is_(True))
            .where(MFAMethod.is_active.is_(True))
        )
        .scalars()
        .all()
    )
    mfa_enabled_reseller = set(
        db.execute(
            select(MFAMethod.subscriber_id)
            .where(MFAMethod.subscriber_id.in_(reseller_ids or [UUID(int=0)]))
            .where(MFAMethod.enabled.is_(True))
            .where(MFAMethod.is_active.is_(True))
        )
        .scalars()
        .all()
    )

    roles_rows = []
    if system_user_ids:
        roles_rows = db.execute(
            select(SystemUserRole, Role)
            .join(Role, Role.id == SystemUserRole.role_id)
            .where(SystemUserRole.system_user_id.in_(system_user_ids))
            .order_by(SystemUserRole.assigned_at.desc())
        ).all()
    role_map: dict = {}
    for user_role, role in roles_rows:
        role_map.setdefault(user_role.system_user_id, []).append(
            {
                "id": str(role.id),
                "name": role.name,
                "is_active": role.is_active,
            }
        )

    users: list[dict[str, Any]] = []
    for row in system_users_rows:
        name = row.display_name or f"{row.first_name} {row.last_name}".strip()
        info = credential_info.get(row.id, {})
        users.append(
            {
                "id": str(row.id),
                "source_type": "system_user",
                "name": name,
                "first_name": row.first_name,
                "last_name": row.last_name,
                "display_name": row.display_name,
                "email": row.email,
                "roles": role_map.get(row.id, []),
                "user_type": row.user_type.value
                if row.user_type
                else UserType.system_user.value,
                "user_type_label": user_type_label(row.user_type),
                "is_active": bool(row.is_active),
                "is_pending": not info.get("has_active")
                or bool(info.get("must_change_password")),
                "mfa_enabled": row.id in mfa_enabled_system,
                "last_login": info.get("last_login"),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "is_bulk_actionable": True,
                "manage_url": f"/admin/system/users/{row.id}",
                "edit_url": f"/admin/system/users/{row.id}/edit",
            }
        )

    reseller_users: list[dict[str, Any]] = []
    for row in reseller_rows:
        name = row.display_name or f"{row.first_name} {row.last_name}".strip()
        info = credential_info.get(row.id, {})
        reseller_user = {
            "id": str(row.id),
            "source_type": "reseller",
            "name": name or row.email,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "display_name": row.display_name,
            "email": row.email,
            "roles": [],
            "user_type": UserType.reseller.value,
            "user_type_label": user_type_label(UserType.reseller),
            "is_active": bool(row.is_active),
            "is_pending": not info.get("has_active")
            or bool(info.get("must_change_password")),
            "mfa_enabled": row.id in mfa_enabled_reseller,
            "last_login": info.get("last_login"),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "is_bulk_actionable": False,
            "manage_url": None,
            "edit_url": None,
            "reseller_id": str(row.reseller_id) if row.reseller_id else None,
            "reseller_detail_url": (
                f"/admin/resellers/{row.reseller_id}" if row.reseller_id else None
            ),
        }
        if _row_matches_filter_query(reseller_user, merged_query):
            reseller_users.append(reseller_user)

    # Cross-source merge (system users + reseller portal users) sorted by an
    # arbitrary key: both populations are loaded COMPLETELY before sorting, so
    # totals and pages are exact — this is not the load-then-slice truncation
    # bug class. Bounded by design: staff + reseller logins are an
    # operator-scale population. Revisit with a merged SQL window only if that
    # assumption breaks.
    combined_users = users + reseller_users
    combined_users.sort(
        key=lambda item: _user_sort_key(item, sort_field), reverse=reverse
    )
    total = len(combined_users)
    paged_users = combined_users[offset : offset + limit]
    return paged_users, total


def list_active_roles(db: Session) -> list[Role]:
    roles = (
        db.execute(
            select(Role)
            .where(Role.is_active.is_(True))
            .order_by(Role.name.asc())
            .limit(500)
        )
        .scalars()
        .all()
    )
    return list(roles)


def build_users_page_state(
    db: Session,
    *,
    search: str | None,
    role: str | None,
    status: str | None,
    filters: str | None,
    order_by: str | None,
    order_dir: str | None,
    offset: int,
    limit: int,
) -> dict[str, object]:
    users, total = list_users(
        db,
        search=search,
        role_id=role,
        status=status,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        offset=offset,
        limit=limit,
    )
    return {
        "users": users,
        "search": search,
        "role": role,
        "status": status,
        "filters": filters,
        "order_by": order_by or "last_name",
        "order_dir": order_dir or "asc",
        "stats": get_user_stats(db),
        "user_kpis": build_user_kpis(db),
        "roles": list_active_roles(db),
        "user_type_options": USER_TYPE_OPTIONS,
        "filter_schema": _serialize_filter_schema(db),
        "pagination": total > limit,
        "total": total,
        "offset": offset,
        "limit": limit,
    }
