import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.db import get_db as _get_db
from app.models.auth import ApiKey, SessionStatus
from app.models.auth import Session as AuthSession
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.services.auth import (
    hash_api_key,
    hash_api_key_candidates,
    is_legacy_api_key_hash,
)
from app.services.auth_flow import (
    _load_rbac_claims,
    decode_access_token,
    hash_session_token,
)

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

# Attribute the permission-guard factories below stamp onto the dependency they
# return, so a static consumer can read WHICH permission a guard demands
# instead of only its callable name. Without it, ``require_permission`` is
# indistinguishable from ``require_permission`` no matter what key it closes
# over — which is exactly how a ``:read`` permission ended up guarding
# ``POST /admin/network/routers/new`` unnoticed. Read it through
# ``permission_requirement()``; never poke the attribute directly.
_PERMISSION_REQUIREMENT_ATTR = "__dotmac_permission_requirement__"


@dataclass(frozen=True)
class PermissionRequirement:
    """The permission keys a guard ACCEPTS, split by HTTP method class.

    ``read_any_of``/``write_any_of`` are *any-of* sets: holding any ONE key in
    the applicable set satisfies the guard. They are therefore the guard's
    authorization FLOOR — a guard is only as strong as its weakest accepted
    key, which is why ``require_any_permission("system:read", ..., "monitoring:read")``
    on a mutating route is a read-tier guard even though it names three domains.

    ``read_methods`` is empty for guards that demand the same thing regardless
    of method; only ``require_method_permission`` populates it.
    """

    read_any_of: tuple[str, ...]
    write_any_of: tuple[str, ...]
    read_methods: frozenset[str] = frozenset()

    def any_of_for(self, method: str) -> tuple[str, ...]:
        """The keys that satisfy this guard for an ``method`` request."""
        if method.upper() in self.read_methods:
            return self.read_any_of
        return self.write_any_of


def _declare_permissions(dependency: _F, requirement: PermissionRequirement) -> _F:
    setattr(dependency, _PERMISSION_REQUIREMENT_ATTR, requirement)
    return dependency


def permission_requirement(dependency: object) -> PermissionRequirement | None:
    """What ``dependency`` demands, or ``None`` if it is not a permission guard."""
    requirement = getattr(dependency, _PERMISSION_REQUIREMENT_ATTR, None)
    return requirement if isinstance(requirement, PermissionRequirement) else None


def _claims_from_payload_or_db(
    db: Session,
    principal_id: str,
    principal_type: str,
    payload: dict[str, Any],
) -> tuple[list[str], list[str]]:
    roles_value = payload.get("roles")
    scopes_value = payload.get("scopes")
    roles = [str(role) for role in roles_value] if isinstance(roles_value, list) else []
    scopes = (
        [str(scope) for scope in scopes_value] if isinstance(scopes_value, list) else []
    )
    if roles or scopes:
        return roles, scopes
    resolved_roles, resolved_scopes = _load_rbac_claims(
        db, principal_type, principal_id
    )
    return list(resolved_roles), list(resolved_scopes)


def claims_for_principal(
    db: Session,
    principal_id: str,
    principal_type: str,
    payload: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Roles/scopes for a principal — token claims first, RBAC tables otherwise.

    Public entry point for auth surfaces that cannot use the HTTP dependency
    stack (e.g. WebSocket handshakes).
    """
    return _claims_from_payload_or_db(db, principal_id, principal_type, payload)


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _is_jwt(token: str) -> bool:
    return token.count(".") == 2


# "Last used" is observability, not an audit trail: refresh at most once per
# window so hot keys don't pay a DB write (and commit) on every request.
_API_KEY_LAST_USED_REFRESH = timedelta(minutes=5)


def _touch_api_key_last_used(db: Session, api_key: ApiKey, now: datetime) -> None:
    last_used = _as_utc(api_key.last_used_at)
    if last_used is not None and now - last_used < _API_KEY_LAST_USED_REFRESH:
        return
    api_key.last_used_at = now
    db.commit()


def _maybe_upgrade_api_key_hash(db: Session, api_key: ApiKey, raw_key: str) -> None:
    """Rehash a legacy (unsalted-sha256) key to HMAC on successful auth.

    One-time per key: after the upgrade the stored hash is no longer legacy, so
    later requests skip this. Committed unconditionally (not subject to the
    last-used throttle) so the upgrade always persists.
    """
    if not is_legacy_api_key_hash(api_key.key_hash):
        return
    upgraded = hash_api_key(raw_key)
    if is_legacy_api_key_hash(upgraded):
        return  # no server secret configured (dev/test) — nothing to upgrade to
    api_key.key_hash = upgraded
    db.commit()


def _has_audit_scope(payload: dict) -> bool:
    scopes: set[str] = set()
    scope_value = payload.get("scope")
    if isinstance(scope_value, str):
        scopes.update(scope_value.split())
    scopes_value = payload.get("scopes")
    if isinstance(scopes_value, list):
        scopes.update(str(item) for item in scopes_value)
    role_value = payload.get("role")
    roles_value = payload.get("roles")
    roles: set[str] = set()
    if isinstance(role_value, str):
        roles.add(role_value)
    if isinstance(roles_value, list):
        roles.update(str(item) for item in roles_value)
    return (
        "audit:read" in scopes
        or "audit:*" in scopes
        or "admin" in roles
        or "auditor" in roles
    )


def require_audit_auth(
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(_get_db),
):
    token = _extract_bearer_token(authorization) or x_session_token
    now = datetime.now(UTC)
    if token:
        if _is_jwt(token):
            payload = decode_access_token(db, token)
            principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
            principal_type = str(payload.get("principal_type") or "subscriber")
            roles, scopes = _claims_from_payload_or_db(
                db, principal_id, principal_type, payload
            )
            effective_payload = {**payload, "roles": roles, "scopes": scopes}
            if not _has_audit_scope(effective_payload):
                raise HTTPException(status_code=403, detail="Insufficient scope")
            session_id = payload.get("session_id")
            if session_id:
                session = db.get(AuthSession, session_id)
                if not session:
                    raise HTTPException(status_code=401, detail="Invalid session")
                if session.status != SessionStatus.active or session.revoked_at:
                    raise HTTPException(status_code=401, detail="Invalid session")
                expires_at = _as_utc(session.expires_at)
                if expires_at and expires_at <= now:
                    raise HTTPException(status_code=401, detail="Session expired")
            actor_id = str(payload.get("sub"))
            if request is not None:
                request.state.actor_id = actor_id
                request.state.actor_type = "user"
            finish_read_transaction(db)
            return {"actor_type": "user", "actor_id": actor_id}
        session = (
            db.query(AuthSession)
            .filter(AuthSession.token_hash == hash_session_token(token))
            .filter(AuthSession.status == SessionStatus.active)
            .filter(AuthSession.revoked_at.is_(None))
            .filter(AuthSession.expires_at > now)
            .first()
        )
        if session:
            session_actor_id = str(session.system_user_id or session.subscriber_id)
            if request is not None:
                request.state.actor_id = session_actor_id
                request.state.actor_type = "user"
            finish_read_transaction(db)
            return {"actor_type": "user", "actor_id": session_actor_id}
    # API keys are accepted only when they carry an explicit audit scope — the
    # same gate JWTs must pass (`_has_audit_scope`). A key with no/other scopes is
    # rejected, closing the historical unscoped-key bypass.
    if x_api_key:
        api_key = (
            db.query(ApiKey)
            .filter(ApiKey.key_hash.in_(hash_api_key_candidates(x_api_key)))
            .filter(ApiKey.is_active.is_(True))
            .filter(ApiKey.revoked_at.is_(None))
            .filter((ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))
            .first()
        )
        if api_key and _has_audit_scope({"scopes": list(api_key.scopes or [])}):
            _maybe_upgrade_api_key_hash(db, api_key, x_api_key)
            _touch_api_key_last_used(db, api_key, now)
            if request is not None:
                request.state.actor_id = str(api_key.id)
                request.state.actor_type = "api_key"
            finish_read_transaction(db)
            return {"actor_type": "api_key", "actor_id": str(api_key.id)}
    raise HTTPException(status_code=401, detail="Unauthorized")


def _machine_principal(
    db: Session, raw_key: str, request: Request | None
) -> dict | None:
    """Authenticate against the kernel's `machine_credentials`, or return None.

    Tried BEFORE the local `api_keys` table so a reissued credential wins while
    both exist. This is a bounded dual-READ, not two writers: nothing here
    writes, and the legacy branch below is deleted once both live credentials
    have been reissued and revoked.

    Machine auth is attempted ONLY when the HMAC key is held. A first version
    let `MachineKeyUnavailableError` propagate, on the reasoning that a missing
    key is a deployment fault and must not be disguised as an invalid
    credential. That reasoning is right about production and wrong here: it
    coupled the LEGACY path's availability to the NEW scheme's configuration,
    so every deployment without the key — including the whole test suite — lost
    the ability to authenticate credentials that never needed it. Thirty-six
    tests across three files failed on exactly that.

    So during the window the key's absence means "machine auth is not
    configured yet", which is a true statement about a migration in progress.
    The property that a missing key must not read as a bad credential is not
    lost, only deferred: when the legacy branch is deleted with the last
    reissued credential, `authenticate_machine` is reached unconditionally and
    the kernel's own error surfaces, which is the right moment for it.
    """
    from dotmac_kernel.machine_auth import (
        MACHINE_KEY_SECRET_NAME,
        UnauthorizedError,
        authenticate_machine,
    )
    from dotmac_kernel.secret_sources import get_secret

    if not get_secret(MACHINE_KEY_SECRET_NAME):
        return None

    try:
        principal = authenticate_machine(db, raw_key)
    except UnauthorizedError:
        # Not a machine credential — or revoked, expired, inactive, or minted
        # for another tenant. The kernel returns ONE message for all of those
        # on purpose, because distinguishing them tells a caller which half of
        # a guess was right. Fall through to the legacy table; if it is not
        # there either, the caller gets the same 401 it always did.
        return None
    actor_id = str(principal.credential_id)
    auth = {
        # A machine principal is not a person. The legacy branch falls back to
        # the key's own id for `subscriber_id`/`person_id`, which meant two
        # different things depending on how the row was made; here both are
        # None and callers that need a human must look elsewhere.
        "subscriber_id": None,
        "person_id": None,
        "principal_id": actor_id,
        "principal_type": "api_key",
        "session_id": None,
        "roles": [],
        "scopes": sorted(principal.scopes),
        "impersonated_by": None,
        "api_key_id": actor_id,
    }
    if request is not None:
        request.state.actor_id = actor_id
        request.state.actor_type = "api_key"
        request.state.auth = auth
    finish_read_transaction(db)
    return auth


def _api_key_principal(
    db: Session, raw_key: str, request: Request | None
) -> dict | None:
    """Authenticate an ``X-Api-Key`` as a first-class principal.

    The key's access is exactly its ``scopes`` (wildcard-aware via
    ``require_permission``); it carries no roles, so there is no admin shortcut.
    Returns the auth dict, or ``None`` if the key is missing/invalid.

    Kernel `machine_credentials` are consulted first; this local `api_keys`
    branch is the legacy half of a bounded migration and goes away with the
    last unreissued credential.
    """
    machine = _machine_principal(db, raw_key, request)
    if machine is not None:
        return machine

    now = datetime.now(UTC)
    api_key = (
        db.query(ApiKey)
        .filter(ApiKey.key_hash.in_(hash_api_key_candidates(raw_key)))
        .filter(ApiKey.is_active.is_(True))
        .filter(ApiKey.revoked_at.is_(None))
        .filter((ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))
        .first()
    )
    if not api_key:
        return None
    _maybe_upgrade_api_key_hash(db, api_key, raw_key)
    _touch_api_key_last_used(db, api_key, now)
    actor_id = str(api_key.id)
    owner = str(api_key.subscriber_id) if api_key.subscriber_id else actor_id
    auth = {
        "subscriber_id": owner,
        "person_id": owner,
        "principal_id": actor_id,
        "principal_type": "api_key",
        "session_id": None,
        "roles": [],
        "scopes": list(api_key.scopes or []),
        "impersonated_by": None,
        "api_key_id": actor_id,
    }
    if request is not None:
        request.state.actor_id = actor_id
        request.state.actor_type = "api_key"
        request.state.auth = auth
    finish_read_transaction(db)
    return auth


def require_user_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment]
    db: Session = Depends(_get_db),
):
    if request is not None:
        existing = getattr(request.state, "auth", None)
        if isinstance(existing, dict) and existing.get("principal_id"):
            return existing

    token = _extract_bearer_token(authorization)
    if not token and request is not None:
        token = request.cookies.get("session_token")
    if not token:
        # No bearer/session token — accept a scoped API key as a principal whose
        # access is exactly its scopes (wildcard-aware). require_admin_web_auth
        # still rejects api_key principals from /admin (staff-only baseline).
        # (isinstance guard: direct (non-DI) callers leave x_api_key as the
        # ``Header`` sentinel rather than a str.)
        if isinstance(x_api_key, str) and x_api_key:
            api_key_auth = _api_key_principal(db, x_api_key, request)
            if api_key_auth is not None:
                return api_key_auth
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_access_token(db, token)
    # Reseller "view as customer" tokens are strictly read-only: any mutation
    # under impersonation is rejected at the door, regardless of endpoint.
    if payload.get("imp"):
        method = getattr(request, "method", None) if request is not None else None
        if method is not None and method.upper() not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(
                status_code=403,
                detail="Read-only impersonation session",
            )
    principal_id = payload.get("principal_id") or payload.get("sub")
    principal_type = payload.get("principal_type") or "subscriber"
    session_id = payload.get("session_id")
    if not principal_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(UTC)
    query = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.expires_at > now)
    )
    if principal_type == "system_user":
        query = query.filter(AuthSession.system_user_id == principal_id)
    elif principal_type == "reseller_user":
        # Layer 3: reseller_user bearer principals key the session on
        # reseller_user_id, not subscriber_id.
        query = query.filter(AuthSession.reseller_user_id == principal_id)
    else:
        query = query.filter(AuthSession.subscriber_id == principal_id)
    session = query.first()
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    roles, scopes = _claims_from_payload_or_db(
        db, str(principal_id), str(principal_type), payload
    )
    actor_id = str(principal_id)
    if request is not None:
        request.state.actor_id = actor_id
        request.state.actor_type = "user"
    finish_read_transaction(db)
    return {
        "subscriber_id": str(principal_id),
        "person_id": str(principal_id),
        "principal_id": str(principal_id),
        "principal_type": str(principal_type),
        "session_id": str(session_id),
        "roles": roles,
        "scopes": scopes,
        "impersonated_by": payload.get("imp_by"),
    }


def user_role_names(db: Session, user_id: object) -> set[str] | None:
    """Role names for a system user — the RBAC owner's identity read.

    Returns None when the user cannot be resolved (or has no roles relation),
    so callers can distinguish "no roles" from "unknown user". Consumers map
    roles to their own visibility; they must not fetch SystemUser directly.
    """
    from app.models.system_user import SystemUser

    if not user_id:
        return None
    sys_user = db.get(SystemUser, str(user_id))
    roles = getattr(sys_user, "roles", None)
    if sys_user is None or roles is None:
        return None
    return {getattr(r, "name", "") for r in roles} if roles else set()


def require_role(role_name: str):
    def _require_role(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        principal_id = auth["principal_id"]
        principal_type = auth.get("principal_type", "subscriber")
        roles = set(auth.get("roles") or [])
        if role_name in roles:
            return auth
        role = (
            db.query(Role)
            .filter(Role.name == role_name)
            .filter(Role.is_active.is_(True))
            .first()
        )
        if not role:
            raise HTTPException(status_code=403, detail="Role not found")
        link: Any | None = None
        if principal_type == "system_user":
            link = (
                db.query(SystemUserRole)
                .filter(SystemUserRole.system_user_id == principal_id)
                .filter(SystemUserRole.role_id == role.id)
                .first()
            )
        else:
            link = (
                db.query(SubscriberRole)
                .filter(SubscriberRole.subscriber_id == principal_id)
                .filter(SubscriberRole.role_id == role.id)
                .first()
            )
        if not link:
            raise HTTPException(status_code=403, detail="Forbidden")
        finish_read_transaction(db)
        return auth

    return _require_role


def _permission_domain_aliases(permission_key: str) -> list[str]:
    """Return explicit compatibility aliases for permission-key transitions.

    Legacy report API-key scopes remain read-only aliases during the granular
    reports migration. They intentionally do not satisfy ``:export``.
    """
    aliases = [permission_key]
    if permission_key.startswith("customer:"):
        aliases.append(permission_key.replace("customer:", "subscriber:", 1))
    elif permission_key.startswith("subscriber:"):
        aliases.append(permission_key.replace("subscriber:", "customer:", 1))
    legacy_report_read_scope = {
        "reports:billing:read": "reports:billing",
        "reports:network:read": "reports:network",
    }.get(permission_key)
    if legacy_report_read_scope:
        aliases.append(legacy_report_read_scope)
    return aliases


def _expand_permission_key_single(permission_key: str) -> list[str]:
    """
    Expand a permission key to aliases for the same permission.

    Permission checks are intentionally exact. A route requiring
    ``billing:invoice:create`` is not satisfied by ``billing:write`` unless the
    principal has an explicit wildcard grant handled by ``_wildcard_ancestors``
    or is in the admin role. This keeps broad/admin-only permissions from
    overriding granular role containers.
    """
    return [permission_key]


def _wildcard_ancestors(permission_key: str) -> list[str]:
    """Wildcard keys that, if HELD, satisfy ``permission_key``.

    ``network:nas:write`` -> ``["*", "network:*", "network:nas:*"]``. A role
    or direct grant of any of these authorizes the requirement, so access can
    be granted by domain (``network:*``) or globally (``*``) instead of
    enumerating every leaf permission.
    """
    parts = permission_key.split(":")
    ancestors = ["*"]
    for i in range(1, len(parts)):
        ancestors.append(":".join(parts[:i]) + ":*")
    return ancestors


def _expand_permission_keys(permission_key: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for alias in _permission_domain_aliases(permission_key):
        for key in _expand_permission_key_single(alias):
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
    # Wildcard grants satisfy any matching requirement.
    for key in _wildcard_ancestors(permission_key):
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def has_permission(auth: dict, db: Session, permission_key: str) -> bool:
    principal_id = auth["principal_id"]
    principal_type = auth.get("principal_type", "subscriber")
    roles = set(auth.get("roles") or [])
    if "admin" in roles:
        return True

    possible_keys = _expand_permission_keys(permission_key)

    scopes = set(auth.get("scopes") or [])
    if scopes & set(possible_keys):
        return True

    permissions = (
        db.query(Permission)
        .filter(Permission.key.in_(possible_keys))
        .filter(Permission.is_active.is_(True))
        .all()
    )
    if not permissions:
        return False

    permission_ids = [p.id for p in permissions]

    has_role_permission: Any | None = None
    has_direct_permission: Any | None = None
    if principal_type == "system_user":
        has_role_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_id)
            .filter(RolePermission.permission_id.in_(permission_ids))
            .filter(Role.is_active.is_(True))
            .first()
        )
        has_direct_permission = (
            db.query(SystemUserPermission)
            .filter(SystemUserPermission.system_user_id == principal_id)
            .filter(SystemUserPermission.permission_id.in_(permission_ids))
            .first()
        )
    else:
        has_role_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_id)
            .filter(RolePermission.permission_id.in_(permission_ids))
            .filter(Role.is_active.is_(True))
            .first()
        )
        has_direct_permission = (
            db.query(SubscriberPermission)
            .filter(SubscriberPermission.subscriber_id == principal_id)
            .filter(SubscriberPermission.permission_id.in_(permission_ids))
            .first()
        )

    return bool(has_role_permission or has_direct_permission)


def effective_permission_keys(auth: dict, db: Session) -> frozenset[str]:
    """All permission keys the principal effectively holds, for UI gating.

    Returns the raw granted keys (scopes + role + direct); the ``admin`` role
    collapses to the ``"*"`` sentinel meaning "everything". A requirement is
    checked by expanding it with ``_expand_permission_keys`` against this set
    (see ``can``), mirroring ``has_permission`` — so the UI can hide what the
    principal cannot do while the route stays the authority.
    """
    roles = set(auth.get("roles") or [])
    if "admin" in roles:
        return frozenset({"*"})
    keys: set[str] = set(auth.get("scopes") or [])
    principal_id = auth["principal_id"]
    principal_type = auth.get("principal_type", "subscriber")
    if principal_type == "system_user":
        role_rows = (
            db.query(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_id)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
        )
        direct_rows = (
            db.query(Permission.key)
            .join(
                SystemUserPermission,
                SystemUserPermission.permission_id == Permission.id,
            )
            .filter(SystemUserPermission.system_user_id == principal_id)
            .filter(Permission.is_active.is_(True))
        )
    else:
        role_rows = (
            db.query(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_id)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
        )
        direct_rows = (
            db.query(Permission.key)
            .join(
                SubscriberPermission,
                SubscriberPermission.permission_id == Permission.id,
            )
            .filter(SubscriberPermission.subscriber_id == principal_id)
            .filter(Permission.is_active.is_(True))
        )
    keys.update(key for (key,) in role_rows.all())
    keys.update(key for (key,) in direct_rows.all())
    return frozenset(keys)


def load_permission_keys(auth: dict, db: Session) -> frozenset[str]:
    """Compute and memoize the effective permission-key set on the request's
    ``auth`` dict, so a later ``can`` check in a template needs no DB access."""
    cached = auth.get("permission_keys")
    if cached is None:
        cached = effective_permission_keys(auth, db)
        auth["permission_keys"] = cached
    return cached


def can(request, permission_key: str) -> bool:
    """UI gate: may the current principal perform ``permission_key``?

    Pure set logic over the keys cached by the route's permission guard on the
    request auth — no DB — so a template can hide actions the principal lacks.
    Denies when the set is absent (e.g. an ungated page); the route remains the
    authority that actually enforces access.
    """
    auth = getattr(getattr(request, "state", None), "auth", None)
    if not isinstance(auth, dict):
        return False
    held = auth.get("permission_keys")
    if held is None:
        return False
    if "*" in held:
        return True
    return bool(set(_expand_permission_keys(permission_key)) & set(held))


def action_permitted(request, action) -> bool:
    """Whether an ``Action`` contract should render for the current principal.

    Combines the owner's eligibility (``allowed``) with the RBAC gate
    (``permission``): a template shows the control only when the action is both
    eligible and permitted. Duck-typed so it needs no import of the contract.
    """
    if not getattr(action, "allowed", False):
        return False
    permission = getattr(action, "permission", None)
    return not permission or can(request, permission)


def _request_id(request: Request | None) -> str | None:
    if request is None:
        return None
    state_request_id = getattr(getattr(request, "state", None), "request_id", None)
    if state_request_id:
        return str(state_request_id)
    return str(request.headers.get("x-request-id") or "") or None


def _enforce_staff_leave_write_guard(
    *,
    db: Session,
    auth: dict,
    request: Request | None,
    permission_key: str,
) -> None:
    if request is None:
        return
    from app.services import erp_staff_access

    restriction = erp_staff_access.staff_write_restricted(
        db,
        auth,
        method=request.method,
    )
    if restriction is None:
        return
    erp_staff_access.audit_denied_write(
        db,
        auth=auth,
        restriction=restriction,
        request_id=_request_id(request),
        permission_key=permission_key,
    )
    db.commit()
    raise HTTPException(
        status_code=403,
        detail="Staff leave restriction permits read-only access.",
    )


def require_permission(permission_key: str):
    def _require_permission(
        request: Request = None,  # type: ignore[assignment]
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        # Cache the principal's effective permissions on the request so the
        # rendered page can hide actions they lack (the route below still
        # authorizes). Admin resolves to "*" without a query.
        load_permission_keys(auth, db)
        roles = set(auth.get("roles") or [])
        if "admin" in roles:
            _enforce_staff_leave_write_guard(
                db=db,
                auth=auth,
                request=request,
                permission_key=permission_key,
            )
            finish_read_transaction(db)
            return auth

        possible_keys = _expand_permission_keys(permission_key)
        scopes = set(auth.get("scopes") or [])
        if scopes & set(possible_keys):
            _enforce_staff_leave_write_guard(
                db=db,
                auth=auth,
                request=request,
                permission_key=permission_key,
            )
            finish_read_transaction(db)
            return auth

        permissions = (
            db.query(Permission)
            .filter(Permission.key.in_(possible_keys))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        if not permissions:
            # This indicates a code/config error - permission key isn't in database
            logger.error(
                "Permission check blocked: permission %r not configured "
                "(seed missing or typo?). Principal: %s/%s",
                permission_key,
                auth.get("principal_type"),
                auth.get("principal_id"),
            )
            raise HTTPException(
                status_code=403,
                detail=f"Permission not found: '{permission_key}' not configured",
            )
        if not has_permission(auth, db, permission_key):
            logger.warning(
                "Permission check failed: forbidden for %s (principal_type=%s principal_id=%s roles=%s scopes=%s)",
                permission_key,
                auth.get("principal_type"),
                auth.get("principal_id"),
                auth.get("roles"),
                auth.get("scopes"),
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        _enforce_staff_leave_write_guard(
            db=db,
            auth=auth,
            request=request,
            permission_key=permission_key,
        )
        finish_read_transaction(db)
        return auth

    return _declare_permissions(
        _require_permission,
        PermissionRequirement(
            read_any_of=(permission_key,), write_any_of=(permission_key,)
        ),
    )


def require_any_permission(*permission_keys: str):
    """Require user to have at least one of the specified permissions."""

    def _require_any_permission(
        request: Request = None,  # type: ignore[assignment]
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        load_permission_keys(auth, db)
        principal_id = auth["principal_id"]
        principal_type = auth.get("principal_type", "subscriber")
        roles = set(auth.get("roles") or [])
        if "admin" in roles:
            _enforce_staff_leave_write_guard(
                db=db,
                auth=auth,
                request=request,
                permission_key="|".join(permission_keys),
            )
            finish_read_transaction(db)
            return auth

        # Expand all permission keys
        all_possible_keys = set()
        for key in permission_keys:
            all_possible_keys.update(_expand_permission_keys(key))
        scopes = set(auth.get("scopes") or [])
        if scopes & all_possible_keys:
            _enforce_staff_leave_write_guard(
                db=db,
                auth=auth,
                request=request,
                permission_key="|".join(permission_keys),
            )
            finish_read_transaction(db)
            return auth

        permissions = (
            db.query(Permission)
            .filter(Permission.key.in_(all_possible_keys))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        if not permissions:
            logger.error(
                "Permission check blocked: none of %s configured (seed missing?)",
                list(permission_keys),
            )
            raise HTTPException(
                status_code=403, detail="Required permissions not configured"
            )

        permission_ids = [p.id for p in permissions]

        # Check if user has any of the matching permissions via roles
        has_role_permission: Any | None = None
        has_direct_permission: Any | None = None
        if principal_type == "system_user":
            has_role_permission = (
                db.query(RolePermission)
                .join(Role, RolePermission.role_id == Role.id)
                .join(SystemUserRole, SystemUserRole.role_id == Role.id)
                .filter(SystemUserRole.system_user_id == principal_id)
                .filter(RolePermission.permission_id.in_(permission_ids))
                .filter(Role.is_active.is_(True))
                .first()
            )
            has_direct_permission = (
                db.query(SystemUserPermission)
                .filter(SystemUserPermission.system_user_id == principal_id)
                .filter(SystemUserPermission.permission_id.in_(permission_ids))
                .first()
            )
        else:
            has_role_permission = (
                db.query(RolePermission)
                .join(Role, RolePermission.role_id == Role.id)
                .join(SubscriberRole, SubscriberRole.role_id == Role.id)
                .filter(SubscriberRole.subscriber_id == principal_id)
                .filter(RolePermission.permission_id.in_(permission_ids))
                .filter(Role.is_active.is_(True))
                .first()
            )
            has_direct_permission = (
                db.query(SubscriberPermission)
                .filter(SubscriberPermission.subscriber_id == principal_id)
                .filter(SubscriberPermission.permission_id.in_(permission_ids))
                .first()
            )

        if not has_role_permission and not has_direct_permission:
            raise HTTPException(status_code=403, detail="Forbidden")
        _enforce_staff_leave_write_guard(
            db=db,
            auth=auth,
            request=request,
            permission_key="|".join(permission_keys),
        )
        finish_read_transaction(db)
        return auth

    return _declare_permissions(
        _require_any_permission,
        PermissionRequirement(
            read_any_of=tuple(permission_keys), write_any_of=tuple(permission_keys)
        ),
    )


def require_method_permission(
    read_permission_key: str,
    write_permission_key: str,
    read_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS"),
):
    """Require read permission for read methods, write permission otherwise."""
    normalized_read_methods = {method.upper() for method in read_methods}
    require_read = require_permission(read_permission_key)
    require_write = require_permission(write_permission_key)

    def _require_method_permission(
        request: Request,
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        if request.method.upper() in normalized_read_methods:
            return require_read(request=None, auth=auth, db=db)
        return require_write(request=request, auth=auth, db=db)

    return _declare_permissions(
        _require_method_permission,
        PermissionRequirement(
            read_any_of=(read_permission_key,),
            write_any_of=(write_permission_key,),
            read_methods=frozenset(normalized_read_methods),
        ),
    )


def grant_scopes_for_permission(auth: dict, db: Session, permission_key: str):
    """Resolve HOW the principal holds ``permission_key``, for object scoping.

    Returns:
      * ``"global"`` — admin, a direct (unscoped) grant, or any global role
        grant → authorized everywhere (historical behaviour).
      * a ``set`` of ``(scope_type, scope_id)`` — the principal holds it ONLY
        through scoped role grants; access is limited to those scopes.
      * ``None`` — the principal does not hold the permission at all.
    """
    roles = set(auth.get("roles") or [])
    if "admin" in roles:
        return "global"

    principal_id = auth["principal_id"]
    principal_type = auth.get("principal_type", "subscriber")
    possible_keys = _expand_permission_keys(permission_key)
    permissions = (
        db.query(Permission)
        .filter(Permission.key.in_(possible_keys))
        .filter(Permission.is_active.is_(True))
        .all()
    )
    if not permissions:
        return None
    permission_ids = [p.id for p in permissions]

    role_assign: Any
    direct_model: Any
    if principal_type == "system_user":
        role_assign = SystemUserRole
        assign_id = SystemUserRole.system_user_id
        direct_model = SystemUserPermission
        direct_id = SystemUserPermission.system_user_id
    else:
        role_assign = SubscriberRole
        assign_id = SubscriberRole.subscriber_id
        direct_model = SubscriberPermission
        direct_id = SubscriberPermission.subscriber_id

    # Direct grants carry no scope — they are global by design.
    has_direct = (
        db.query(direct_model)
        .filter(direct_id == principal_id)
        .filter(direct_model.permission_id.in_(permission_ids))
        .first()
    )
    if has_direct is not None:
        return "global"

    rows = (
        db.query(role_assign.scope_type, role_assign.scope_id)
        .join(Role, role_assign.role_id == Role.id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .filter(assign_id == principal_id)
        .filter(RolePermission.permission_id.in_(permission_ids))
        .filter(Role.is_active.is_(True))
        .all()
    )
    if not rows:
        return None
    scopes: set[tuple[str, str]] = set()
    for scope_type, scope_id in rows:
        if not scope_type:  # global role grant
            return "global"
        scopes.add((scope_type, scope_id))
    return scopes


def require_scoped_permission(permission_key: str, scope_extractor):
    """Permission guard that honours object scoping.

    A global grant authorizes everywhere; a region/reseller-scoped grant only
    authorizes resources in that scope. ``scope_extractor(request, db)`` must
    return the accessed resource's ``(scope_type, scope_id)`` (or ``None`` for
    a genuinely scope-less resource, which a scoped-only principal cannot
    reach). Admins and globally-granted principals never invoke the extractor.
    """

    def _require_scoped_permission(
        request: Request,
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        # Scoped routes render the same permission-aware UI controls as routes
        # protected by require_permission. Cache the effective keys here too so
        # `can` and `action_permitted` see the authorization already established
        # by this dependency instead of failing closed because the cache is absent.
        load_permission_keys(auth, db)
        decision = grant_scopes_for_permission(auth, db, permission_key)
        if decision is None:
            raise HTTPException(status_code=403, detail="Forbidden")
        if decision == "global":
            _enforce_staff_leave_write_guard(
                db=db,
                auth=auth,
                request=request,
                permission_key=permission_key,
            )
            return auth
        resource_scope = scope_extractor(request, db)
        if resource_scope is not None and tuple(resource_scope) in decision:
            _enforce_staff_leave_write_guard(
                db=db,
                auth=auth,
                request=request,
                permission_key=permission_key,
            )
            return auth
        raise HTTPException(
            status_code=403, detail="Forbidden for this region/reseller"
        )

    return _declare_permissions(
        _require_scoped_permission,
        PermissionRequirement(
            read_any_of=(permission_key,), write_any_of=(permission_key,)
        ),
    )
