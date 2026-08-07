"""Service helpers for admin RADIUS server/client web routes."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar, cast

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import ConnectionType, NasDevice, NasVendor, RadiusProfile
from app.models.radius import RadiusClient, RadiusServer
from app.models.radius_active_session import RadiusActiveSession
from app.models.radius_error import RadiusAuthError, RadiusAuthErrorType
from app.schemas.catalog import (
    RadiusAttributeCreate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
)
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
)
from app.services import catalog as catalog_service
from app.services import radius as radius_service
from app.services.audit_helpers import (
    build_audit_activities_for_types,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.common import coerce_uuid
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)

logger = logging.getLogger(__name__)

RADIUS_CLIENT_EXCLUDE_FIELDS = {"shared_secret_hash"}
RADIUS_OVERVIEW_PAGE_SIZE = 10

RADIUS_CLIENT_LIST_DEFINITION = ListDefinition(
    key="admin.radius.clients",
    fields=(ListFieldDefinition("client_ip", "Client IP", sortable=True),),
    default_sort="client_ip",
    default_sort_dir="asc",
    default_per_page=RADIUS_OVERVIEW_PAGE_SIZE,
    per_page_options=(RADIUS_OVERVIEW_PAGE_SIZE,),
)

RADIUS_PROFILE_LIST_DEFINITION = ListDefinition(
    key="admin.radius.profiles",
    fields=(ListFieldDefinition("name", "Name", sortable=True),),
    default_sort="name",
    default_sort_dir="asc",
    default_per_page=RADIUS_OVERVIEW_PAGE_SIZE,
    per_page_options=(RADIUS_OVERVIEW_PAGE_SIZE,),
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RadiusOverviewQuery:
    clients: ListQuery
    profiles: ListQuery

    @classmethod
    def from_pages(
        cls, *, clients_page: int = 1, profiles_page: int = 1
    ) -> RadiusOverviewQuery:
        return cls(
            clients=RADIUS_CLIENT_LIST_DEFINITION.build_query(
                search=None,
                filters={},
                page=clients_page,
            ),
            profiles=RADIUS_PROFILE_LIST_DEFINITION.build_query(
                search=None,
                filters={},
                page=profiles_page,
            ),
        )


@dataclass(frozen=True, slots=True)
class RadiusOverviewListPage(Generic[T]):
    items: tuple[T, ...]
    page_meta: PageMeta


@dataclass(frozen=True, slots=True)
class RadiusOverviewActivity:
    title: str
    description: str | None
    occurred_at: datetime | None


@dataclass(frozen=True, slots=True)
class RadiusOverviewPage:
    servers: tuple[RadiusServer, ...]
    clients: RadiusOverviewListPage[RadiusClient]
    profiles: RadiusOverviewListPage[RadiusProfile]
    recent_sessions: tuple[RadiusActiveSession, ...]
    recent_errors: tuple[RadiusAuthError, ...]
    activities: tuple[RadiusOverviewActivity, ...]
    total_online: int
    total_errors: int


@dataclass
class RadiusFormResult:
    success: bool
    form_context: dict[str, object] | None = None
    error: str | None = None
    not_found_message: str | None = None


def _actor_id_from_request(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    subscriber_id = current_user.get("subscriber_id")
    return str(subscriber_id) if subscriber_id else None


def _log_radius_audit_event(
    db: Session,
    request: Request,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, object] | None,
) -> None:
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
    )


def _validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        return str(errors[0].get("msg") or "Please correct the highlighted fields.")
    return "Please correct the highlighted fields."


def active_servers(db: Session) -> list[RadiusServer]:
    """Return active RADIUS servers for client form select options."""
    return cast(
        list[RadiusServer],
        db.query(RadiusServer)
        .filter(RadiusServer.is_active.is_(True))
        .order_by(RadiusServer.name)
        .all(),
    )


def parse_server_form(form) -> dict[str, object]:
    """Parse RADIUS server form values."""
    return {
        "name": form.get("name", "").strip(),
        "host": form.get("host", "").strip(),
        "auth_port_raw": form.get("auth_port", "").strip(),
        "acct_port_raw": form.get("acct_port", "").strip(),
        "description": form.get("description", "").strip(),
        "is_active": form.get("is_active") == "true",
    }


def validate_server_form(values: dict[str, object]) -> str | None:
    """Validate required RADIUS server fields."""
    if not values.get("name"):
        return "Server name is required."
    if not values.get("host"):
        return "Host is required."
    return None


def build_server_payload(
    values: dict[str, object], *, current_server
) -> tuple[RadiusServerUpdate | None, str | None]:
    """Build server update payload and validate optional ports."""
    auth_port = current_server.auth_port
    acct_port = current_server.acct_port
    auth_port_raw = str(values.get("auth_port_raw") or "")
    acct_port_raw = str(values.get("acct_port_raw") or "")
    try:
        if auth_port_raw:
            auth_port = int(auth_port_raw)
        if acct_port_raw:
            acct_port = int(acct_port_raw)
    except ValueError:
        return None, "Auth and accounting ports must be valid integers."
    return (
        RadiusServerUpdate(
            name=str(values.get("name") or ""),
            host=str(values.get("host") or ""),
            auth_port=auth_port,
            acct_port=acct_port,
            description=(str(values.get("description") or "") or None),
            is_active=bool(values.get("is_active")),
        ),
        None,
    )


def server_form_data(values: dict[str, object], *, current_server) -> dict[str, object]:
    """Build template-friendly server data for re-renders."""
    auth_port_raw = str(values.get("auth_port_raw") or "")
    acct_port_raw = str(values.get("acct_port_raw") or "")
    return {
        "id": current_server.id,
        "name": values.get("name"),
        "host": values.get("host"),
        "auth_port": auth_port_raw or str(current_server.auth_port),
        "acct_port": acct_port_raw or str(current_server.acct_port),
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }


def build_server_create_payload(
    values: dict[str, object],
) -> tuple[RadiusServerCreate | None, str | None]:
    auth_port_raw = str(values.get("auth_port_raw") or "")
    acct_port_raw = str(values.get("acct_port_raw") or "")
    try:
        auth_port = int(auth_port_raw) if auth_port_raw else 1812
        acct_port = int(acct_port_raw) if acct_port_raw else 1813
    except ValueError:
        return None, "Auth and accounting ports must be valid integers."
    return (
        RadiusServerCreate(
            name=str(values.get("name") or ""),
            host=str(values.get("host") or ""),
            auth_port=auth_port,
            acct_port=acct_port,
            description=(str(values.get("description") or "") or None),
            is_active=bool(values.get("is_active")),
        ),
        None,
    )


def server_create_form_data(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values.get("name"),
        "host": values.get("host"),
        "auth_port": str(values.get("auth_port_raw") or "1812"),
        "acct_port": str(values.get("acct_port_raw") or "1813"),
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }


def parse_client_form(form) -> dict[str, object]:
    """Parse RADIUS client form values."""
    return {
        "server_id": form.get("server_id", "").strip(),
        "client_ip": form.get("client_ip", "").strip(),
        "shared_secret": form.get("shared_secret", ""),
        "description": form.get("description", "").strip(),
        "is_active": form.get("is_active") == "true",
    }


def validate_client_form(
    values: dict[str, object], *, require_secret: bool
) -> str | None:
    """Validate required client fields."""
    if not values.get("server_id"):
        return "RADIUS server is required."
    if not values.get("client_ip"):
        return "Client IP address is required."
    if require_secret and not values.get("shared_secret"):
        return "Shared secret is required."
    return None


def build_client_update_payload(values: dict[str, object]) -> RadiusClientUpdate:
    """Build client update payload."""
    server_id_raw = str(values.get("server_id") or "")
    payload_data = {
        "server_id": coerce_uuid(server_id_raw) if server_id_raw else None,
        "client_ip": str(values.get("client_ip") or "") or None,
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }
    shared_secret = str(values.get("shared_secret") or "")
    if shared_secret:
        payload_data["shared_secret_hash"] = hashlib.sha256(
            shared_secret.encode("utf-8")
        ).hexdigest()
    return RadiusClientUpdate.model_validate(payload_data)


def client_form_data(
    values: dict[str, object], *, client_id: str | None = None
) -> dict[str, object]:
    """Build template-friendly client form data for re-renders."""
    data = {
        "server_id": values.get("server_id"),
        "client_ip": values.get("client_ip"),
        "description": (str(values.get("description") or "") or None),
        "is_active": bool(values.get("is_active")),
    }
    if client_id:
        data["id"] = client_id
    return data


def radius_page_data(db: Session, *, query: RadiusOverviewQuery) -> RadiusOverviewPage:
    servers = tuple(
        radius_service.radius_servers.list(
            db=db,
            is_active=None,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
    )

    clients_total = int(
        db.scalar(
            select(func.count(RadiusClient.id)).where(RadiusClient.is_active.is_(True))
        )
        or 0
    )
    clients_page_meta = PageMeta.from_query(query.clients, clients_total)
    clients_query = query.clients.with_page(clients_page_meta.page)
    clients = tuple(
        radius_service.radius_clients.list(
            db=db,
            server_id=None,
            is_active=None,
            order_by=clients_query.sort_by,
            order_dir=clients_query.sort_dir,
            limit=clients_query.per_page,
            offset=clients_query.offset,
        )
    )

    profiles_total = int(
        db.scalar(
            select(func.count(RadiusProfile.id)).where(
                RadiusProfile.is_active.is_(True)
            )
        )
        or 0
    )
    profiles_page_meta = PageMeta.from_query(query.profiles, profiles_total)
    profiles_query = query.profiles.with_page(profiles_page_meta.page)
    profiles = tuple(
        catalog_service.radius_profiles.list(
            db=db,
            vendor=None,
            is_active=None,
            order_by=profiles_query.sort_by,
            order_dir=profiles_query.sort_dir,
            limit=profiles_query.per_page,
            offset=profiles_query.offset,
        )
    )

    recent_sessions = tuple(
        db.scalars(
            select(RadiusActiveSession)
            .options(
                joinedload(RadiusActiveSession.subscriber),
                joinedload(RadiusActiveSession.nas_device),
            )
            .order_by(RadiusActiveSession.session_start.desc())
            .limit(5)
        )
        .unique()
        .all()
    )
    recent_errors = tuple(
        db.scalars(
            select(RadiusAuthError)
            .order_by(RadiusAuthError.occurred_at.desc())
            .limit(5)
        ).all()
    )
    activity_rows = build_audit_activities_for_types(
        db=db,
        entity_types=["radius_server", "radius_client", "radius_profile"],
        limit=10,
    )
    activities = tuple(
        RadiusOverviewActivity(
            title=str(row.get("title") or "Activity"),
            description=str(row.get("description") or "") or None,
            occurred_at=(
                row.get("occurred_at")
                if isinstance(row.get("occurred_at"), datetime)
                else None
            ),
        )
        for row in activity_rows
    )

    return RadiusOverviewPage(
        servers=servers,
        clients=RadiusOverviewListPage(
            items=clients,
            page_meta=clients_page_meta,
        ),
        profiles=RadiusOverviewListPage(
            items=profiles,
            page_meta=profiles_page_meta,
        ),
        recent_sessions=recent_sessions,
        recent_errors=recent_errors,
        activities=activities,
        total_online=int(db.scalar(select(func.count(RadiusActiveSession.id))) or 0),
        total_errors=int(db.scalar(select(func.count(RadiusAuthError.id))) or 0),
    )


def import_credentials_notice(db: Session) -> str:
    result = radius_service.import_access_credentials_from_external_radius(db)
    return (
        "Imported RADIUS credentials: "
        f"scanned {result['scanned']}, created {result['created']}, "
        f"updated {result['updated']}, unmatched {result['unmatched']}, "
        f"conflicts {result['conflicts']}."
    )


def server_new_form_data() -> dict[str, object]:
    return {
        "server": None,
        "action_url": "/admin/network/radius/servers",
    }


def server_edit_form_data(db: Session, server_id: str) -> dict[str, object] | None:
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        return None
    return {
        "server": server,
        "action_url": f"/admin/network/radius/servers/{server_id}",
    }


def server_create_form_context(
    values: dict[str, object], *, error: str | None = None
) -> dict[str, object]:
    context: dict[str, object] = {
        "server": server_create_form_data(values),
        "action_url": "/admin/network/radius/servers",
    }
    if error:
        context["error"] = error
    return context


def server_update_form_context(
    values: dict[str, object],
    *,
    current_server,
    server_id: str,
    error: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "server": server_form_data(values, current_server=current_server),
        "action_url": f"/admin/network/radius/servers/{server_id}",
    }
    if error:
        context["error"] = error
    return context


def create_server_from_form(
    request: Request,
    db: Session,
    form,
) -> RadiusFormResult:
    values = parse_server_form(form)
    error = validate_server_form(values)
    payload = None
    if not error:
        payload, error = build_server_create_payload(values)

    if error:
        return RadiusFormResult(
            success=False,
            form_context=server_create_form_context(values, error=error),
            error=error,
        )

    try:
        if payload is None:
            raise ValueError("Please correct the highlighted fields.")
        server = radius_service.radius_servers.create(db=db, payload=payload)
        _log_radius_audit_event(
            db,
            request,
            action="create",
            entity_type="radius_server",
            entity_id=str(server.id),
            metadata={"name": server.name, "host": server.host},
        )
        return RadiusFormResult(success=True)
    except ValidationError as exc:
        error = _validation_error_message(exc)
    except Exception as exc:
        error = str(exc)

    return RadiusFormResult(
        success=False,
        form_context=server_create_form_context(
            values, error=error or "Please correct the highlighted fields."
        ),
        error=error,
    )


def update_server_from_form(
    request: Request,
    db: Session,
    *,
    server_id: str,
    form,
) -> RadiusFormResult:
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        return RadiusFormResult(
            success=False, not_found_message="RADIUS server not found"
        )

    before_snapshot = model_to_dict(server)
    values = parse_server_form(form)
    error = validate_server_form(values)
    payload = None
    if not error:
        payload, error = build_server_payload(values, current_server=server)

    if error:
        return RadiusFormResult(
            success=False,
            form_context=server_update_form_context(
                values,
                current_server=server,
                server_id=server_id,
                error=error,
            ),
            error=error,
        )

    try:
        if payload is None:
            raise ValueError("Please correct the highlighted fields.")
        updated_server = radius_service.radius_servers.update(
            db=db,
            server_id=server_id,
            payload=payload,
        )
        changes = diff_dicts(before_snapshot, model_to_dict(updated_server))
        _log_radius_audit_event(
            db,
            request,
            action="update",
            entity_type="radius_server",
            entity_id=str(updated_server.id),
            metadata={"changes": changes} if changes else None,
        )
        return RadiusFormResult(success=True)
    except ValidationError as exc:
        error = _validation_error_message(exc)
    except Exception as exc:
        error = str(exc)

    return RadiusFormResult(
        success=False,
        form_context=server_update_form_context(
            values,
            current_server=server,
            server_id=server_id,
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def client_create_form_context(
    db: Session, values: dict[str, object], *, error: str | None = None
) -> dict[str, object]:
    context: dict[str, object] = {
        "client": client_form_data(values),
        "servers": active_servers(db),
        "action_url": "/admin/network/radius/clients",
    }
    if error:
        context["error"] = error
    return context


def client_update_form_context(
    db: Session,
    values: dict[str, object],
    *,
    client_id: str,
    error: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "client": client_form_data(values, client_id=client_id),
        "servers": active_servers(db),
        "action_url": f"/admin/network/radius/clients/{client_id}",
    }
    if error:
        context["error"] = error
    return context


def client_new_form_data(db: Session) -> dict[str, object]:
    return {
        "client": None,
        "servers": active_servers(db),
        "action_url": "/admin/network/radius/clients",
    }


def client_edit_form_data(db: Session, client_id: str) -> dict[str, object] | None:
    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        return None
    return {
        "client": client,
        "servers": active_servers(db),
        "action_url": f"/admin/network/radius/clients/{client_id}",
    }


def create_client_from_form(
    request: Request,
    db: Session,
    form,
) -> RadiusFormResult:
    values = parse_client_form(form)
    error = validate_client_form(values, require_secret=True)
    if error:
        return RadiusFormResult(
            success=False,
            form_context=client_create_form_context(db, values, error=error),
            error=error,
        )

    try:
        client = radius_service.radius_clients.create(
            db=db,
            payload=build_client_create_payload(values),
        )
        _log_radius_audit_event(
            db,
            request,
            action="create",
            entity_type="radius_client",
            entity_id=str(client.id),
            metadata={
                "client_ip": client.client_ip,
                "server_id": str(client.server_id),
            },
        )
        return RadiusFormResult(success=True)
    except ValidationError as exc:
        error = _validation_error_message(exc)
    except Exception as exc:
        error = str(exc)

    return RadiusFormResult(
        success=False,
        form_context=client_create_form_context(
            db, values, error=error or "Please correct the highlighted fields."
        ),
        error=error,
    )


def update_client_from_form(
    request: Request,
    db: Session,
    *,
    client_id: str,
    form,
) -> RadiusFormResult:
    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        return RadiusFormResult(
            success=False, not_found_message="RADIUS client not found"
        )

    before_snapshot = model_to_dict(client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
    values = parse_client_form(form)
    error = validate_client_form(values, require_secret=False)
    if error:
        return RadiusFormResult(
            success=False,
            form_context=client_update_form_context(
                db,
                values,
                client_id=str(client.id),
                error=error,
            ),
            error=error,
        )

    try:
        updated_client = radius_service.radius_clients.update(
            db=db,
            client_id=client_id,
            payload=build_client_update_payload(values),
        )
        after_snapshot = model_to_dict(
            updated_client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS
        )
        changes = diff_dicts(before_snapshot, after_snapshot)
        _log_radius_audit_event(
            db,
            request,
            action="update",
            entity_type="radius_client",
            entity_id=str(updated_client.id),
            metadata={"changes": changes} if changes else None,
        )
        return RadiusFormResult(success=True)
    except ValidationError as exc:
        error = _validation_error_message(exc)
    except Exception as exc:
        error = str(exc)

    return RadiusFormResult(
        success=False,
        form_context=client_update_form_context(
            db,
            values,
            client_id=str(client.id),
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def active_sessions_page_data(
    db: Session,
    *,
    search: str = "",
    nas_filter: str = "",
) -> dict[str, object]:
    stmt = (
        select(RadiusActiveSession)
        .options(
            joinedload(RadiusActiveSession.subscriber),
            joinedload(RadiusActiveSession.nas_device),
        )
        .order_by(RadiusActiveSession.session_start.desc())
    )
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            RadiusActiveSession.username.ilike(pattern)
            | RadiusActiveSession.framed_ip_address.ilike(pattern)
        )
    if nas_filter:
        stmt = stmt.where(RadiusActiveSession.nas_device_id == nas_filter)

    return {
        "sessions": db.scalars(stmt.limit(500)).unique().all(),
        "total_online": db.scalar(select(func.count(RadiusActiveSession.id))) or 0,
        "nas_devices": db.scalars(select(NasDevice).order_by(NasDevice.name)).all(),
        "search": search,
        "nas_filter": nas_filter,
        "snapshot_at": datetime.now(UTC),
    }


def radius_auth_errors_page_data(
    db: Session,
    *,
    error_type: str = "",
    page: int = 1,
) -> dict[str, Any]:
    per_page = 50
    stmt = select(RadiusAuthError).order_by(RadiusAuthError.occurred_at.desc())
    if error_type:
        try:
            stmt = stmt.where(
                RadiusAuthError.error_type == RadiusAuthErrorType(error_type)
            )
        except ValueError:
            pass

    total = db.scalar(select(func.count(RadiusAuthError.id))) or 0
    type_counts = db.execute(
        select(RadiusAuthError.error_type, func.count(RadiusAuthError.id)).group_by(
            RadiusAuthError.error_type
        )
    ).all()

    return {
        "errors": db.scalars(stmt.limit(per_page).offset((page - 1) * per_page)).all(),
        "total": total,
        "type_counts": type_counts,
        "error_types": [e.value for e in RadiusAuthErrorType],
        "selected_error_type": error_type,
        "page": page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
    }


def profile_vendors() -> list[str]:
    return [item.value for item in NasVendor]


def profile_connection_types() -> list[str]:
    return [item.value for item in ConnectionType]


def profile_new_form_data() -> dict[str, object]:
    return {
        "profile": None,
        "attributes": [],
        "vendors": profile_vendors(),
        "connection_types": profile_connection_types(),
        "action_url": "/admin/network/radius/profiles",
    }


def profile_edit_form_data(db, profile_id: str) -> dict[str, object] | None:
    try:
        profile = catalog_service.radius_profiles.get(db=db, profile_id=profile_id)
    except Exception:
        return None
    attributes = catalog_service.radius_attributes.list(
        db=db,
        profile_id=profile_id,
        order_by="attribute",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "profile": profile,
        "attributes": attributes,
        "vendors": profile_vendors(),
        "connection_types": profile_connection_types(),
        "action_url": f"/admin/network/radius/profiles/{profile_id}",
    }


def parse_profile_attributes(form) -> tuple[list[dict], str | None]:
    names = form.getlist("attribute_name")
    operators = form.getlist("attribute_operator")
    values = form.getlist("attribute_value")
    max_len = max(len(names), len(operators), len(values))
    attrs: list[dict] = []
    for idx in range(max_len):
        name = (names[idx] if idx < len(names) else "").strip()
        value = (values[idx] if idx < len(values) else "").strip()
        operator = (operators[idx] if idx < len(operators) else "").strip() or None
        if not name and not value:
            continue
        if not name or not value:
            return [], "Each RADIUS attribute row needs both an attribute and a value."
        attrs.append({"attribute": name, "operator": operator, "value": value})
    return attrs, None


def parse_profile_form(form) -> tuple[dict[str, object], list[dict], str | None]:
    name = form.get("name", "").strip()
    vendor = form.get("vendor", "").strip()
    connection_type = form.get("connection_type", "").strip()
    description = form.get("description", "").strip()
    download_speed = form.get("download_speed", "").strip()
    upload_speed = form.get("upload_speed", "").strip()
    mikrotik_rate_limit = form.get("mikrotik_rate_limit", "").strip()
    mikrotik_address_list = form.get("mikrotik_address_list", "").strip()
    is_active = form.get("is_active") == "true"
    attributes, attr_error = parse_profile_attributes(form)

    profile_data = {
        "name": name,
        "description": description or None,
        "is_active": is_active,
    }
    if vendor:
        profile_data["vendor"] = vendor
    if connection_type:
        profile_data["connection_type"] = connection_type
    if download_speed:
        try:
            profile_data["download_speed"] = int(download_speed)
        except ValueError:
            return profile_data, attributes, "Download speed must be a valid integer."
    if upload_speed:
        try:
            profile_data["upload_speed"] = int(upload_speed)
        except ValueError:
            return profile_data, attributes, "Upload speed must be a valid integer."
    if mikrotik_rate_limit:
        profile_data["mikrotik_rate_limit"] = mikrotik_rate_limit
    profile_data["mikrotik_address_list"] = mikrotik_address_list or None
    # Framed-Pool (v4) and Delegated-IPv6-Prefix-Pool (v6) names. Set
    # unconditionally so an admin can clear them by submitting an empty field.
    profile_data["ip_pool_name"] = form.get("ip_pool_name", "").strip() or None
    profile_data["ipv6_pool_name"] = form.get("ipv6_pool_name", "").strip() or None

    if not name:
        return profile_data, attributes, "Profile name is required."
    if attr_error:
        return profile_data, attributes, attr_error
    return profile_data, attributes, None


def create_profile(db, profile_data: dict[str, object], attributes: list[dict]):
    payload = RadiusProfileCreate.model_validate(profile_data)
    profile = catalog_service.radius_profiles.create(db=db, payload=payload)
    for attr in attributes:
        catalog_service.radius_attributes.create(
            db=db,
            payload=RadiusAttributeCreate(profile_id=profile.id, **attr),
        )
    metadata = {
        "name": profile.name,
        "vendor": profile.vendor.value if profile.vendor else None,
        "attributes": {"from": 0, "to": len(attributes)},
    }
    return profile, metadata


def profile_form_context(
    profile_data: dict[str, object],
    attributes: list[dict],
    *,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "profile": profile_data,
        "attributes": attributes,
        "vendors": profile_vendors(),
        "connection_types": profile_connection_types(),
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def create_profile_from_form(
    request: Request,
    db: Session,
    form,
) -> RadiusFormResult:
    profile_data, attributes, error = parse_profile_form(form)
    action_url = "/admin/network/radius/profiles"
    if error:
        return RadiusFormResult(
            success=False,
            form_context=profile_form_context(
                profile_data,
                attributes,
                action_url=action_url,
                error=error,
            ),
            error=error,
        )

    try:
        profile, metadata = create_profile(db, profile_data, attributes)
        _log_radius_audit_event(
            db,
            request,
            action="create",
            entity_type="radius_profile",
            entity_id=str(profile.id),
            metadata=metadata,
        )
        return RadiusFormResult(success=True)
    except ValidationError as exc:
        error = _validation_error_message(exc)
    except Exception as exc:
        error = str(exc)

    return RadiusFormResult(
        success=False,
        form_context=profile_form_context(
            profile_data,
            attributes,
            action_url=action_url,
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def update_profile(
    db,
    *,
    profile_id: str,
    profile_data: dict[str, object],
    attributes: list[dict],
):
    profile = catalog_service.radius_profiles.get(db=db, profile_id=profile_id)
    before_snapshot = model_to_dict(profile)
    existing = catalog_service.radius_attributes.list(
        db=db,
        profile_id=profile_id,
        order_by="attribute",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    before_attr_count = len(existing)

    payload = RadiusProfileUpdate.model_validate(profile_data)
    updated_profile = catalog_service.radius_profiles.update(
        db=db,
        profile_id=profile_id,
        payload=payload,
    )
    for attr in existing:
        catalog_service.radius_attributes.delete(db=db, attribute_id=str(attr.id))
    for attr in attributes:
        catalog_service.radius_attributes.create(
            db=db,
            payload=RadiusAttributeCreate(profile_id=profile.id, **attr),
        )

    after_snapshot = model_to_dict(updated_profile)
    changes = diff_dicts(before_snapshot, after_snapshot)
    after_attr_count = len(attributes)
    if before_attr_count != after_attr_count:
        changes["attributes"] = {"from": before_attr_count, "to": after_attr_count}
    metadata = {"changes": changes} if changes else None
    return updated_profile, metadata


def update_profile_from_form(
    request: Request,
    db: Session,
    *,
    profile_id: str,
    form,
) -> RadiusFormResult:
    if not profile_edit_form_data(db, profile_id):
        return RadiusFormResult(
            success=False, not_found_message="RADIUS profile not found"
        )

    profile_data, attributes, error = parse_profile_form(form)
    action_url = f"/admin/network/radius/profiles/{profile_id}"
    if error:
        return RadiusFormResult(
            success=False,
            form_context=profile_form_context(
                profile_data,
                attributes,
                action_url=action_url,
                error=error,
            ),
            error=error,
        )

    try:
        updated_profile, metadata = update_profile(
            db=db,
            profile_id=profile_id,
            profile_data=profile_data,
            attributes=attributes,
        )
        _log_radius_audit_event(
            db,
            request,
            action="update",
            entity_type="radius_profile",
            entity_id=str(updated_profile.id),
            metadata=metadata,
        )
        return RadiusFormResult(success=True)
    except ValidationError as exc:
        error = _validation_error_message(exc)
    except Exception as exc:
        error = str(exc)

    return RadiusFormResult(
        success=False,
        form_context=profile_form_context(
            profile_data,
            attributes,
            action_url=action_url,
            error=error or "Please correct the highlighted fields.",
        ),
        error=error,
    )


def build_client_create_payload(values: dict[str, object]) -> RadiusClientCreate:
    server_id_raw = str(values.get("server_id") or "")
    client_ip = str(values.get("client_ip") or "")
    return RadiusClientCreate.model_validate(
        {
            "server_id": coerce_uuid(server_id_raw),
            "client_ip": client_ip,
            "shared_secret_hash": hashlib.sha256(
                str(values.get("shared_secret") or "").encode("utf-8")
            ).hexdigest(),
            "description": (str(values.get("description") or "") or None),
            "is_active": bool(values.get("is_active")),
        }
    )
