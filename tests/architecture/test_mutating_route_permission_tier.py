"""A mutating route must be guarded by a WRITE-tier permission, not a read one.

``test_admin_web_route_guards`` and ``test_route_permission_guards`` both ask
only "is there a guard?" — their ``_GUARD_NAMES`` sets match on the dependency
CALLABLE's name, and ``require_permission("router:read")`` and
``require_permission("router:write")`` produce the same callable name
(``_require_permission``). So a ``:read`` permission on a POST satisfied both
tests. That is how ``POST /admin/network/routers/new`` (``create_router``) and
``POST /admin/network/routers/{router_id}/edit`` (``update_router``) shipped
guarded by nothing but a router-level ``router:read``, alongside siblings in the
same file that correctly used ``router:write``.

This test closes that CLASS. For every mutating route on the staff surfaces
(``/admin`` and ``/api/v1``) it resolves the route's effective permission floor
and fails the build unless at least one guard demands a permission that is not
read-tier.

Three properties make it actually bite:

1. **The guard set is the UNION of route-level, router-level and mount-point
   dependencies.** FastAPI flattens all three into ``route.dependant``, and this
   test reads that tree rather than the decorator. A per-route source scan
   cannot see ``APIRouter(dependencies=[...])`` — which is precisely the shape
   the ``network_routers`` defect had (no route-level guard at all), and the
   shape ``POST /api/v1/geocode/preview`` still has (its only permission comes
   from the ``readperm:`` mount mode in ``app.main``).
2. **It walks the MOUNT TABLE, not one router object.** The surface is rebuilt
   from ``_CORE_ROUTER_SPECS`` + ``_DEFERRED_API_ROUTER_SPECS`` through the real
   ``_mount_router``. ``app.web.admin.network_routers`` used to be mounted
   out-of-band under ``/admin`` — where ``_mount_router`` drops mount
   dependencies entirely — so it received no staff baseline and neither
   architecture test could see it. A router that is mounted is audited, however
   it got there.
3. **Read-tier is decided by the VERB SEGMENT, matched segment-exactly.**
   ``"foo:read_write"`` is a write; ``"foo:read"`` is a read. Substring matching
   would get that backwards. A permission whose verb is in NEITHER vocabulary
   below fails the build rather than passing silently — a new verb must be
   classified deliberately.

Scope: this test audits routes that carry at least one permission guard and asks
whether that guard is strong enough. Routes with NO permission guard at all are
the sibling tests' subject (both keep their own empty ``_KNOWN_UNGUARDED``
quarantines and self-scoped allowlists); together the three cover the surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from app.main import (
    _CORE_ROUTER_SPECS,
    _DEFERRED_API_ROUTER_SPECS,
    _load_router_object,
    _mount_router,
)
from app.services.auth_dependencies import permission_requirement, require_permission

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_AUDITED_PREFIXES = ("/admin", "/api/v1")

# Verb segments (the LAST ``:``-separated segment of a permission key) that
# authorize reading only. Matched segment-exactly, never as a substring.
# ``access``/``list`` are not in Sub's seeded catalogue today; they are listed
# because a key spelled that way would be a read, and classifying it late is
# how a hole opens.
_READ_TIER_VERBS = frozenset(
    {
        "read",
        "view",  # gis:fiber:view, gis:map:view
        "export",  # gis:export, reports:billing:export, reports:network:export
        "check",  # gis:serviceability:check
        "access",
        "list",
    }
)

# Verb segments that authorize a change. Every permission key appearing on a
# mutating route must be in one vocabulary or the other — see
# ``test_every_permission_on_a_mutating_route_has_a_classified_verb``.
_WRITE_TIER_VERBS = frozenset(
    {
        "accept",
        "activate",
        "admin",
        "apply",
        "archive",
        "assign",
        "attention",
        "billing_write",
        "cancel",
        "commission",
        "configure",
        "create",
        "db_admin",
        "delete",
        "dispatch",
        "distribute",
        "edit",
        "impersonate",
        "manage",
        "membership",
        "mirror",
        "pay",
        "push_config",
        "redrive",
        "retire",
        "reverse",
        "review",
        "self_assign",
        "send",
        "service_change_reconcile",
        "suspend",
        "update",
        "use",
        "verify",
        "waive",
        "write",
    }
)

# ── Allowlist 1: POSTs that are reads ────────────────────────────────────────
# A POST body is the only way to send a structured request (a filter set, a
# serial number, a template's variables) or to avoid putting a device serial in
# a URL/access log. These routes change nothing durable, so a read permission is
# the CORRECT guard for them, not a tolerated one. Each entry says why.
_POST_SHAPED_READS: dict[str, str] = {
    # -- network device diagnostics: talk to the device, persist nothing ------
    "POST /api/v1/olt-devices/{olt_id}/ont-status-by-serial": (
        "Looks up one ONT's live status by serial on the OLT. Serial is in the "
        "body, not the path, so it stays out of access logs."
    ),
    "POST /api/v1/olt-devices/{olt_id}/test-connection": (
        "Opens an SSH session to the OLT and reports reachability. No row, no "
        "file, no device change."
    ),
    "POST /api/v1/ont-units/{ont_id}/running-config": (
        "Reads the ONT's running config back over the management channel."
    ),
    "POST /api/v1/ont-units/{ont_id}/diagnostics/ping": (
        "Runs a ping from the ONT and returns the result. Target and count are "
        "request inputs; nothing is stored."
    ),
    "POST /api/v1/ont-units/{ont_id}/diagnostics/traceroute": (
        "Traceroute counterpart of the ping diagnostic above."
    ),
    "POST /api/v1/network/routers/{router_id}/test-connection": (
        "RouterOS reachability probe. Returns a ConnectionTestResult and "
        "persists nothing."
    ),
    "POST /api/v1/network/routers/config-templates/{template_id}/preview": (
        "Renders a config template against caller-supplied variables and "
        "returns the text. Nothing is pushed to a device or saved."
    ),
    "POST /admin/network/olts/{olt_id}/ont-status-by-serial": (
        "Admin-surface twin of the ONT-status-by-serial API read above."
    ),
    "POST /admin/network/olts/{olt_id}/ssh-get-config": (
        "Fetches the OLT running config for on-screen display "
        "(``fetch_running_config_ssh_preview``). Deliberately distinct from "
        "``/config-backup``, which persists an artefact and takes the write "
        "tier."
    ),
    "POST /admin/network/olts/{olt_id}/tr069-profiles": (
        "Reads TR-069 server profiles off the OLT over SSH into an HTMX "
        "partial. The POST that CREATES a profile is separately write-guarded."
    ),
    "POST /admin/network/olts/{olt_id}/profiles/imported/audit": (
        "Live dependency audit of imported profile mappings — a comparison "
        "rendered into a partial."
    ),
    "POST /admin/network/olts/{olt_id}/profiles/offer-sync/preview": (
        "Dry-run of the OLT profile commands an offer would generate. The "
        "commands are shown, never sent."
    ),
    "POST /admin/network/onts/{ont_id}/ping-diagnostic": (
        "Admin-surface twin of the ONT ping diagnostic."
    ),
    "POST /admin/network/onts/{ont_id}/traceroute-diagnostic": (
        "Admin-surface twin of the ONT traceroute diagnostic."
    ),
    "POST /admin/network/routers/{router_id}/test-connection": (
        "Admin-surface twin of the RouterOS reachability probe."
    ),
    # -- previews / simulations: compute the outcome, apply nothing ----------
    "POST /api/v1/geocode/preview": (
        "Geocodes an address supplied in the body and returns candidates. Its "
        "only guard comes from the ``readperm:gis:serviceability:check`` mount "
        "mode — proof that this test must resolve mount-point dependencies."
    ),
    "POST /api/v1/uptime-reports": (
        "Computes an uptime report over a body-supplied window from alert "
        "history. A report is returned, never stored."
    ),
    "POST /admin/catalog/offers/{offer_id}/fup/simulate": (
        "Simulates FUP rules against a hypothetical usage scenario and returns JSON."
    ),
    "POST /admin/catalog/subscriptions/{subscription_id}/lifecycle/preview": (
        "Previews one lifecycle command without mutating subscription state, "
        "and additionally re-checks the per-kind permission in the body "
        "(``_assert_lifecycle_preview_permission``)."
    ),
    "POST /admin/catalog/subscriptions/bulk/lifecycle/preview": (
        "Batch counterpart of the lifecycle preview; no item is mutated."
    ),
    "POST /admin/provisioning/bulk-activate/preview": (
        "Builds the bulk-activation preview from posted filters/mapping. The "
        "apply route is separately write-guarded."
    ),
    "POST /admin/provisioning/migrate/preview": (
        "Builds the service-migration preview from posted filters/targets."
    ),
    "POST /admin/crm/inbox/ai-intake-policy/{version_id}/preview": (
        "Simulates an AI intake policy version against a sample customer "
        "message. ``preview_policy_version`` writes nothing."
    ),
    "POST /admin/inbox/manager-ai": (
        "Asks a question over inbox data the caller may already read. Guarded "
        "by the purpose-built ``support:inbox_ai:read``; no domain state "
        "changes."
    ),
    # -- guarded in the body, per action -------------------------------------
    "POST /admin/billing/invoices/bulk/review/{action}": (
        "The dependency is only the entry check: the real authorization is "
        "``_require_action_permission`` in the body, which resolves the "
        "action's OWN permission from its definition and 403s without it. The "
        "route itself only renders the review screen."
    ),
    "POST /admin/billing/invoices/bulk/confirm/{action}": (
        "Same per-action in-body re-check as the review route, plus "
        "``_require_confirmed_invoice_scope``. The dependency cannot name the "
        "permission because the action is a path parameter."
    ),
}

# ── Allowlist 2: read-tier guards on routes that DO persist ─────────────────
# These are not reads. Each writes a durable artefact of a read. They are listed
# separately, and NOT described as reads, because the honest premise here is
# "the owner has not chosen a write-tier permission for this yet" — not "this is
# safe". Naming them keeps them visible instead of laundering them through the
# reads list above. Every entry needs an owner decision; the list may only
# shrink.
_READ_TIER_ARTEFACT_WRITES: dict[str, str] = {
    "POST /admin/reports/insight/{advisor_key}": (
        "Persists an AIInsight row (committed in ``intelligence_engine.advise``) "
        "and spends the daily LLM token budget, under ``provisioning:read`` — a "
        "UI-assignable read, and the wrong domain for a support/SLA report. "
        "Left as-is here because no write-tier permission in the seeded "
        "catalogue fits 'generate an advisory insight'; introducing one is a "
        "seed + role-grant change, not a guard edit. OWNER DECISION NEEDED."
    ),
    "POST /admin/sales/quotes/{quote_id}/pdf": (
        "``generate_quote_pdf`` records a quote export before streaming it. The "
        "artefact is provenance of a read the caller is already entitled to "
        "(``crm:quote:read``), so the exposure is storage, not data. OWNER "
        "DECISION NEEDED on whether exporting is its own grant."
    ),
    "POST /admin/system/export/download": (
        "Creates an export job row and enqueues a Celery task once the row "
        "count crosses the background threshold. Guarded by "
        "``system:settings:read``, which is admin-only in the seeded catalogue "
        "(``ADMIN_ONLY_PERMISSION_KEYS``) — but that is DB state a runtime RBAC "
        "edit can flip, so it is not an enforceable premise. Separately worth "
        "an owner decision: this exports subscriber data under a SETTINGS "
        "permission."
    ),
}

# ── Allowlist 3: explicitly governed read-authorized mutations ───────────────
# These routes genuinely change state, but their reviewed product contract
# deliberately grants the subordinate mutation to every principal authorized to
# read the exact parent resource. Keep this category separate from POST-shaped
# reads so the durable write is never misrepresented.
_READ_AUTHORIZED_MUTATIONS: dict[str, str] = {
    "POST /admin/dispatch/work-orders/{work_order_id}/expenses": (
        "Creates a requester-owned claim bound to the exact work order. The "
        "reviewed work-order expense contract grants this action to every staff "
        "principal who can read that work order. The command owner rechecks exact "
        "work-order authorization, current technician assignment, CSRF, category "
        "rules, receipts, and idempotency before writing."
    ),
}

_ALLOWLIST: dict[str, str] = {
    **_POST_SHAPED_READS,
    **_READ_TIER_ARTEFACT_WRITES,
    **_READ_AUTHORIZED_MUTATIONS,
}


def _build_full_surface() -> FastAPI:
    """Rebuild the true production surface from the mount table.

    Both spec lists, every mount kind — so a router mounted out-of-band under
    ``/admin`` is audited exactly like one reached through ``app.web.admin``.
    """
    surface = FastAPI()
    for module_name, attr_name, mount_kind, mode in (
        _CORE_ROUTER_SPECS + _DEFERRED_API_ROUTER_SPECS
    ):
        router = _load_router_object(module_name, attr_name)
        _mount_router(surface, router, mount_kind, mode)
    return surface


def _dependency_callables(dependant) -> list:
    """Every callable in the route's dependency tree.

    FastAPI merges route-level, router-level and mount-point dependencies into
    this one tree, which is what makes the union resolution above real.
    """
    callables = []
    call = getattr(dependant, "call", None)
    if call is not None:
        callables.append(call)
    for sub in getattr(dependant, "dependencies", []) or []:
        callables.extend(_dependency_callables(sub))
    return callables


def _is_read_tier(permission_key: str) -> bool:
    verb = permission_key.rsplit(":", 1)[-1]
    return verb in _READ_TIER_VERBS


def _audited_routes(app: FastAPI):
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if not path.startswith(_AUDITED_PREFIXES):
            continue
        methods = set(route.methods or []) & _MUTATING_METHODS
        if not methods:
            continue
        yield route, methods, path


def _read_tier_violations(app: FastAPI) -> set[str]:
    """Mutating routes whose whole permission floor is read-tier."""
    violations: set[str] = set()
    for route, methods, path in _audited_routes(app):
        callables = _dependency_callables(route.dependant)
        requirements = [
            requirement
            for requirement in (permission_requirement(call) for call in callables)
            if requirement is not None
        ]
        # A role guard (``require_role("admin")``) is not permission-tiered at
        # all; it is strictly stronger than any read permission, so it settles
        # the question.
        if any(getattr(call, "__name__", "") == "_require_role" for call in callables):
            continue
        if not requirements:
            # No permission guard at all: the sibling guard tests own this case.
            continue
        for method in sorted(methods):
            # A guard is satisfied by ANY ONE of its keys, so it only forces a
            # write when EVERY key it accepts is write-tier. That is what makes
            # require_any_permission("system:read", ..., "monitoring:read") a
            # read-tier guard on a POST.
            if any(
                requirement.any_of_for(method)
                and all(
                    not _is_read_tier(key) for key in requirement.any_of_for(method)
                )
                for requirement in requirements
            ):
                continue
            violations.add(f"{method} {path}")
    return violations


def test_every_mutating_route_requires_a_write_tier_permission():
    violations = _read_tier_violations(_build_full_surface())

    unexpected = sorted(violations - set(_ALLOWLIST))
    assert not unexpected, (
        "These mutating routes are guarded only by read-tier permissions, so a "
        "principal holding nothing but a `:read` grant can change state through "
        "them. Move the route to the write-tier permission its own module "
        "already uses. A reviewed product contract that deliberately permits a "
        "read-authorized mutation must be added to _READ_AUTHORIZED_MUTATIONS "
        "with its safety reason:\n  " + "\n  ".join(unexpected)
    )

    # The other direction of the ratchet: an allowlisted route that is no longer
    # reported has been fixed, moved or deleted, and its entry must go with it.
    # Without this the list would silently accumulate permission to be wrong.
    stale = sorted(set(_ALLOWLIST) - violations)
    assert not stale, (
        "These routes are allowlisted but no longer read-tier-guarded (fixed, "
        "renamed or removed). Delete their entries — the allowlist may only "
        "shrink:\n  " + "\n  ".join(stale)
    )


def test_every_permission_on_a_mutating_route_has_a_classified_verb():
    """No permission may be tiered by accident.

    Read-tier is decided by the verb segment; a key whose verb is in neither
    vocabulary would fall through to "not read-tier" and silently satisfy the
    gate. Make that a build failure instead, so a new verb is classified by a
    human on the change that introduces it.
    """
    unclassified: dict[str, set[str]] = {}
    for route, methods, path in _audited_routes(_build_full_surface()):
        for call in _dependency_callables(route.dependant):
            requirement = permission_requirement(call)
            if requirement is None:
                continue
            keys = set(requirement.read_any_of) | set(requirement.write_any_of)
            for key in keys:
                verb = key.rsplit(":", 1)[-1]
                if verb in _READ_TIER_VERBS or verb in _WRITE_TIER_VERBS:
                    continue
                unclassified.setdefault(key, set()).add(f"{sorted(methods)[0]} {path}")

    assert not unclassified, (
        "These permission keys guard a mutating route but their verb segment is "
        "in neither _READ_TIER_VERBS nor _WRITE_TIER_VERBS, so the read/write "
        "gate cannot tier them. Classify each verb:\n  "
        + "\n  ".join(
            f"{key} (e.g. {sorted(routes)[0]})"
            for key, routes in sorted(unclassified.items())
        )
    )


# ── Sensitivity proof ────────────────────────────────────────────────────────
# A check over a set that happens to be empty passes for the wrong reason. These
# plant each shape the detector must catch, assert it is reported, then assert
# the write-tier version of the same route goes quiet. Both directions matter:
# a detector that reports everything is as useless as one that reports nothing.


def _surface_with(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_detector_catches_a_read_guard_declared_on_the_route():
    planted = APIRouter(prefix="/admin/__planted__")

    @planted.post("/mutate", dependencies=[Depends(require_permission("router:read"))])
    def _planted_read() -> None:  # pragma: no cover - never called
        return None

    assert _read_tier_violations(_surface_with(planted)) == {
        "POST /admin/__planted__/mutate"
    }


def test_detector_catches_a_read_guard_declared_on_the_ROUTER():
    """The defect-1 shape: the POST itself declares no guard at all."""
    planted = APIRouter(
        prefix="/admin/__planted__",
        dependencies=[Depends(require_permission("router:read"))],
    )

    @planted.post("/mutate")
    def _planted_router_level() -> None:  # pragma: no cover - never called
        return None

    assert _read_tier_violations(_surface_with(planted)) == {
        "POST /admin/__planted__/mutate"
    }


def test_detector_catches_a_read_guard_supplied_at_the_MOUNT_POINT():
    """The ``readperm:`` mount-mode shape, as on ``/api/v1/geocode/preview``."""
    planted = APIRouter(prefix="/admin/__planted__")

    @planted.post("/mutate")
    def _planted_mount_level() -> None:  # pragma: no cover - never called
        return None

    app = FastAPI()
    app.include_router(
        planted, dependencies=[Depends(require_permission("router:read"))]
    )
    assert _read_tier_violations(app) == {"POST /admin/__planted__/mutate"}


def test_detector_catches_an_any_permission_guard_with_one_read_alternative():
    """One read alternative is enough: any-of guards are only as strong as their
    weakest key. This is the ``/admin/alerts`` defect shape."""
    from app.services.auth_dependencies import require_any_permission

    planted = APIRouter(prefix="/admin/__planted__")

    @planted.post(
        "/mutate",
        dependencies=[
            Depends(require_any_permission("monitoring:write", "monitoring:read"))
        ],
    )
    def _planted_any() -> None:  # pragma: no cover - never called
        return None

    assert _read_tier_violations(_surface_with(planted)) == {
        "POST /admin/__planted__/mutate"
    }


def test_detector_goes_quiet_on_the_write_tier_versions():
    """Remove the defect and the report must be empty — otherwise the assertions
    above would pass on a detector that simply flags every mutating route."""
    from app.services.auth_dependencies import require_any_permission

    route_level = APIRouter(prefix="/admin/__planted__")

    @route_level.post(
        "/mutate", dependencies=[Depends(require_permission("router:write"))]
    )
    def _quiet_route() -> None:  # pragma: no cover - never called
        return None

    router_level = APIRouter(
        prefix="/admin/__planted2__",
        dependencies=[Depends(require_permission("router:write"))],
    )

    @router_level.post("/mutate")
    def _quiet_router() -> None:  # pragma: no cover - never called
        return None

    any_of = APIRouter(prefix="/admin/__planted3__")

    @any_of.post(
        "/mutate",
        dependencies=[
            Depends(require_any_permission("monitoring:write", "system:write"))
        ],
    )
    def _quiet_any() -> None:  # pragma: no cover - never called
        return None

    for planted in (route_level, router_level, any_of):
        assert _read_tier_violations(_surface_with(planted)) == set()


def test_read_tier_matching_is_segment_exact_not_substring():
    """``foo:read_write`` is a write. A substring match would call it a read."""
    assert _is_read_tier("router:read") is True
    assert _is_read_tier("router:write") is False
    assert _is_read_tier("router:read_write") is False
    assert _is_read_tier("billing:ledger:read") is True
    assert _is_read_tier("catalog:billing_write") is False
    assert _is_read_tier("gis:serviceability:check") is True


def test_every_permission_guard_factory_declares_what_it_demands():
    """The gate's own blind spot, closed.

    Tiering a route depends on ``permission_requirement()`` being able to read
    the key a guard closed over, and that only works because each factory stamps
    a ``PermissionRequirement`` onto the dependency it returns. A NEW factory
    added without the stamp would not make this gate fail — it would make the
    routes it guards look unguarded, and the gate skips those. That is a silent
    hole, so assert the property directly on the source: every module-level
    ``require_*permission*`` factory must return through
    ``_declare_permissions``.
    """
    import ast
    import inspect
    import pathlib

    from app.services import auth_dependencies

    source_path = pathlib.Path(inspect.getsourcefile(auth_dependencies) or "")
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    undeclared: list[str] = []
    factories: list[str] = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not (node.name.startswith("require_") and "permission" in node.name):
            continue
        factories.append(node.name)
        # Only the factory's OWN returns; the nested dependency's returns live
        # one scope down and are not part of this contract.
        returns = [stmt for stmt in node.body if isinstance(stmt, ast.Return)]
        if not returns or not all(
            isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", "") == "_declare_permissions"
            for stmt in returns
        ):
            undeclared.append(node.name)

    # Sensitivity: an empty scan would pass this test for the wrong reason.
    assert len(factories) >= 4, (
        "Found no permission guard factories to check — the discovery rule "
        f"(module-level `require_*permission*`) has drifted. Saw: {factories}"
    )
    assert not undeclared, (
        "These permission guard factories return a dependency that does not "
        "carry a PermissionRequirement, so "
        "test_every_mutating_route_requires_a_write_tier_permission cannot see "
        "which permission they demand and will silently skip every route they "
        "guard. Wrap the returned dependency in _declare_permissions(...):\n  "
        + "\n  ".join(undeclared)
    )
