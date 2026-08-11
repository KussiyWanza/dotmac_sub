import asyncio
import logging
import os
import secrets
import warnings
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from importlib import import_module
from threading import Lock
from time import monotonic
from typing import TypedDict

warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    module=r"routeros_api\.sentence",
)

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    generate_csrf_token,
    set_csrf_cookie,
)
from app.errors import register_error_handlers
from app.logging import configure_logging
from app.models.domain_settings import DomainSetting, SettingDomain
from app.monitoring import setup_monitoring
from app.observability import ObservabilityMiddleware
from app.request_meta import client_ip
from app.services import audit as audit_service
from app.services.db_session_adapter import db_session_adapter
from app.telemetry import setup_otel

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session

# Make the white-label `brand` value available to every Jinja2 template. Must run
# before the (lazily imported) web routers create their Jinja2Templates instances.
from app.ui import UI_ASSET_DIRECTORY, UI_ASSET_MOUNT  # noqa: E402
from app.web.brand_globals import install_brand_jinja_global  # noqa: E402

install_brand_jinja_global()

_AUDIT_SETTINGS_CACHE: dict | None = None
_AUDIT_SETTINGS_CACHE_AT: float | None = None
_AUDIT_SETTINGS_CACHE_TTL_SECONDS = 30.0
_AUDIT_SETTINGS_REFRESH_TIMEOUT_MS = 1000
_AUDIT_SETTINGS_LOCK = Lock()
_AUDIT_ALWAYS_SKIP_EXACT_PATHS = {"/health", "/metrics"}
_AUDIT_ALWAYS_SKIP_PATH_PREFIXES = (
    "/static/",
    "/api/v1/webhooks/",
    "/api/v1/alerts/",
)
_DEFERRED_ROUTER_TASK = None
_DEFERRED_STARTUP_TASK = None

_CORE_ROUTER_SPECS = [
    ("app.api.health", "router", "api", "none"),
    ("app.api.tr069_auth", "router", "api", "none"),
    ("app.api.tr069_inform", "router", "api", "none"),
    ("app.api.reconcile_webhooks", "router", "api", "none"),
    # Inbound provider webhooks must be mounted before serving — if they were
    # deferred, they 404 during the startup load window and we'd silently drop
    # payment confirmations / monitoring alerts on every restart.
    ("app.api.billing", "webhook_router", "api", "none"),
    ("app.api.crm_webhooks", "router", "api", "none"),
    ("app.api.inbox_webhooks", "router", "api", "none"),
    ("app.api.meta_inbox_webhooks", "router", "api", "none"),
    ("app.api.fiber_inquiry_webhooks", "router", "api", "none"),
    ("app.api.erp_material_webhooks", "router", "api", "none"),
    ("app.api.lead_capture_webhooks", "router", "api", "none"),
    ("app.api.chat_widget", "router", "web", "none"),
    ("app.api.crm", "router", "api", "none"),
    ("app.api.search", "router", "api", "readperm:customer:read"),
    ("app.api.network_ont_ops", "router", "api", "user"),
    ("app.api.network_olt_ops", "router", "api", "user"),
    ("app.web.auth", "router", "web", "none"),
]

_DEFERRED_API_ROUTER_SPECS = [
    ("app.web.admin", "router", "web", "none"),
    ("app.web_home", "router", "web", "none"),
    ("app.web_domains", "router", "web", "none"),
    ("app.web.customer", "router", "web", "none"),
    ("app.web.reseller", "router", "web", "none"),
    ("app.web.vendor_auth", "router", "web", "none"),
    ("app.web.vendor_portal", "router", "web", "none"),
    ("app.web.public", "router", "web", "none"),
    ("app.web.admin.network_routers", "router", "admin", "none"),
    ("app.websocket.router", "router", "ws", "none"),
    ("app.websocket.workqueue_router", "router", "ws", "none"),
    ("app.api.notifications", "router", "api", "perm:monitoring"),
    ("app.api.external", "router", "api", "admin"),
    ("app.api.billing", "router", "api", "user"),
    ("app.api.billing_treatments", "router", "api", "user"),
    ("app.api.files", "router", "api", "admin"),
    ("app.api.catalog", "router", "api", "user"),
    ("app.api.auth", "router", "api", "admin"),
    ("app.api.auth_flow", "router", "api", "none"),
    ("app.api.ticket_confirm", "router", "api", "none"),
    # Customer self-care: self-scoped reads, auth-only (no staff permission).
    ("app.api.me", "router", "api", "user"),
    ("app.api.reseller", "router", "api", "user"),
    ("app.api.payment_proofs", "router", "api", "user"),
    ("app.api.service_requests", "router", "api", "user"),
    ("app.api.rbac", "router", "api", "user"),
    ("app.api.staff_sync", "router", "api", "user"),
    ("app.api.customers", "router", "api", "user"),
    ("app.api.subscribers", "router", "api", "user"),
    # Native referrals: staff surface rides crm:lead:* per-route
    # permissions; capture and signed-context signup are public continuations
    # from shared /r/{code} links.
    ("app.api.crm_referrals", "router", "api", "user"),
    ("app.api.crm_referrals", "public_router", "api", "none"),
    ("app.api.support", "router", "api", "user"),
    # Native sales vertical: per-route crm:lead / crm:quote /
    # crm:sales_order permission guards on top of the user-auth base.
    ("app.api.crm_sales", "router", "api", "user"),
    ("app.api.customer_experience", "router", "api", "user"),
    ("app.api.sales", "router", "api", "user"),
    ("app.api.sales_orders", "router", "api", "user"),
    # Native projects vertical.
    ("app.api.projects", "router", "api", "user"),
    ("app.api.dispatch", "router", "api", "perm:operations:dispatch:read"),
    ("app.api.field.config", "router", "api", "none"),
    ("app.api.field", "router", "api", "user"),
    ("app.api.vendor_portal", "router", "api", "user"),
    ("app.api.tables", "router", "api", "user"),
    ("app.api.domains_provisioning", "router", "api", "user"),
    ("app.api.domains_monitoring", "router", "api", "user"),
    ("app.api.domains_network_access", "router", "api", "user"),
    ("app.api.network_device_groups", "router", "api", "user"),
    ("app.api.network_catalog", "router", "api", "user"),
    ("app.api.domains_network_fiber", "router", "api", "user"),
    ("app.api.asset_inventory", "router", "api", "user"),
    ("app.api.domains_usage", "router", "api", "user"),
    ("app.api.imports", "router", "api", "perm:system:settings"),
    ("app.api.audit", "router", "api", "none"),
    ("app.api.gis", "router", "api", "admin"),
    ("app.api.geocoding", "router", "api", "readperm:gis:serviceability:check"),
    ("app.api.qualification", "router", "api", "perm:provisioning"),
    ("app.api.settings", "router", "api", "perm:system:settings"),
    ("app.api.branding", "router", "api", "perm:system:settings"),
    ("app.api.connectors", "router", "api", "admin"),
    ("app.api.integrations", "router", "api", "admin"),
    ("app.api.scheduler", "router", "api", "user"),
    ("app.api.comms", "router", "api", "admin"),
    ("app.api.campaigns", "router", "api", "admin"),
    # One-click unsubscribe is followed from a mail client with no session.
    ("app.api.campaigns", "public_router", "api", "none"),
    ("app.api.ai_operations", "router", "api", "admin"),
    ("app.api.workqueue", "router", "api", "user"),
    ("app.api.analytics", "router", "api", "admin"),
    ("app.api.fiber_plant", "router", "api", "perm:network:fiber"),
    ("app.api.vendor_routes", "router", "api", "perm:network:fiber"),
    ("app.api.nextcloud_talk", "router", "api", "admin"),
    ("app.api.wireguard", "router", "api", "perm:network:vpn"),
    ("app.api.nas", "router", "api", "perm:network:nas"),
    ("app.api.router_management", "router", "api", "user"),
    ("app.api.router_management", "jump_host_router", "api", "user"),
    ("app.api.provisioning", "router", "api", "user"),
    ("app.api.bandwidth", "router", "api", "perm:monitoring"),
    ("app.api.validation", "router", "api", "admin"),
    ("app.api.defaults", "router", "api", "perm:system:settings"),
    ("app.api.wireguard", "public_router", "api", "none"),
]


def _get_release_metadata() -> dict[str, str | None]:
    return {
        "release": os.getenv("APP_RELEASE")
        or os.getenv("IMAGE_TAG")
        or os.getenv("GIT_SHA"),
        "git_sha": os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA"),
        "environment": os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "unknown",
    }


def _log_release_metadata(component: str) -> None:
    metadata = _get_release_metadata()
    logger.info(
        "application_release",
        extra={
            "event": "application_release",
            "component": component,
            **metadata,
        },
    )


def _router_dependencies(mode: str):
    if mode == "user":
        from app.api.deps import require_user_auth

        return [Depends(require_user_auth)]
    if mode == "admin":
        from app.api.deps import require_role

        return [Depends(require_role("admin"))]
    # "perm:<domain>" guards the whole router with method-aware permissions:
    # GET/HEAD/OPTIONS need <domain>:read, mutations need <domain>:write.
    if mode.startswith("perm:"):
        from app.services.auth_dependencies import require_method_permission

        domain = mode[len("perm:") :]
        return [Depends(require_method_permission(f"{domain}:read", f"{domain}:write"))]
    # "readperm:<key>" guards a read-only router with a single permission.
    if mode.startswith("readperm:"):
        from app.services.auth_dependencies import require_permission

        return [Depends(require_permission(mode[len("readperm:") :]))]
    return None


def _load_router_object(module_name: str, attr_name: str):
    module = import_module(module_name)
    return getattr(module, attr_name)


def _apply_router_spec(app: FastAPI, spec: tuple[str, str, str, str]) -> None:
    module_name, attr_name, mount_kind, dependency_mode = spec
    router = _load_router_object(module_name, attr_name)
    _mount_router(app, router, mount_kind, dependency_mode)


def _mount_router(app: FastAPI, router, mount_kind: str, dependency_mode: str) -> None:
    dependencies = _router_dependencies(dependency_mode)

    if mount_kind == "api":
        app.include_router(router, prefix="/api/v1", dependencies=dependencies)
        return
    if mount_kind == "admin":
        app.include_router(router, prefix="/admin")
        return
    app.include_router(router)


def _include_core_routers(app: FastAPI) -> None:
    for spec in _CORE_ROUTER_SPECS:
        _apply_router_spec(app, spec)


async def _load_deferred_api_routers(app: FastAPI) -> None:
    logger.info(
        "deferred_api_router_load_begin",
        extra={
            "event": "deferred_api_router_load_begin",
            "router_count": len(_DEFERRED_API_ROUTER_SPECS),
        },
    )
    for spec in _DEFERRED_API_ROUTER_SPECS:
        module_name, attr_name, _mount_kind, _dependency_mode = spec
        try:
            router = await asyncio.to_thread(
                _load_router_object, module_name, attr_name
            )
            _mount_router(app, router, spec[2], spec[3])
            logger.info(
                "deferred_api_router_loaded",
                extra={
                    "event": "deferred_api_router_loaded",
                    # NOTE: 'module' is a reserved LogRecord attribute; using
                    # router_module instead avoids KeyError on every iteration.
                    "router_module": module_name,
                    "attr": attr_name,
                },
            )
        except Exception:
            logger.exception(
                "deferred_api_router_load_failed",
                extra={
                    "event": "deferred_api_router_load_failed",
                    "router_module": module_name,
                    "attr": attr_name,
                },
            )
            # Continue loading the rest of the routers rather than aborting
            # the entire deferred load — a single broken module shouldn't
            # take down the customer/reseller portals.
            continue
        await asyncio.sleep(0)
    logger.info(
        "deferred_api_router_load_complete",
        extra={"event": "deferred_api_router_load_complete"},
    )


def _warn_on_scheduler_registry_drift() -> None:
    try:
        from app.celery_app import celery_app
        from app.services.scheduler_config import find_unregistered_scheduled_tasks

        drift = find_unregistered_scheduled_tasks(celery_app.tasks.keys())
    except Exception:
        logger.warning(
            "scheduler_registry_drift_check_failed",
            exc_info=True,
            extra={"event": "scheduler_registry_drift_check_failed"},
        )
        return

    if not drift:
        logger.info(
            "scheduler_registry_drift_check_clean",
            extra={"event": "scheduler_registry_drift_check_clean"},
        )
        return

    logger.warning(
        "scheduler_registry_drift_detected",
        extra={
            "event": "scheduler_registry_drift_detected",
            "unknown_task_count": len(drift),
            "unknown_tasks": [item["task_name"] for item in drift],
        },
    )


def _assert_required_schema() -> None:
    """Fail fast when required DB schema changes are missing."""
    db = SessionLocal()
    try:
        inspector = sqlalchemy_inspect(db.get_bind())
        if not inspector.has_table("ont_units"):
            raise RuntimeError(
                "Database schema is incompatible: required table 'ont_units' is missing. "
                "Run `alembic upgrade head` before starting the app."
            )
        ont_columns = {column["name"] for column in inspector.get_columns("ont_units")}
        if "contact" not in ont_columns:
            raise RuntimeError(
                "Database schema is incompatible: required column 'ont_units.contact' is missing. "
                "Run `alembic upgrade head` before starting the app."
            )
    finally:
        db.close()


def _check_test_environment_leakage() -> None:
    """Warn if test environment variables are set in production.

    The PYTEST_CURRENT_TEST variable being set in production can cause
    test stubs/mocks to be used instead of real implementations, leading
    to errors like '_RedisStub' object has no attribute 'get'.
    """
    test_env_vars = [
        "PYTEST_CURRENT_TEST",
        "PYTEST_VERSION",
        "_PYTEST_RAISE",
    ]
    found = []
    for var in test_env_vars:
        if os.environ.get(var):
            found.append(var)

    if found:
        logger.error(
            "test_environment_variables_detected",
            extra={
                "event": "test_environment_variables_detected",
                "variables": found,
                "warning": "Test environment variables are set in production. "
                "This can cause test stubs to be used instead of real implementations.",
            },
        )


def _seed_startup_settings() -> None:
    started_at = monotonic()
    logger.info("startup_seed_begin", extra={"event": "startup_seed_begin"})

    # Check for test environment leakage (can cause _RedisStub errors)
    _check_test_environment_leakage()

    # Enforce credential encryption if configured (P0 security fix)
    from app.config import settings
    from app.services.credential_crypto import require_encryption_key
    from app.services.object_storage import (
        ObjectStorageConnectionError,
        ObjectStorageError,
        ensure_storage_bucket,
    )
    from app.services.settings_seed import (
        seed_audit_settings,
        seed_auth_policy_settings,
        seed_auth_settings,
        seed_billing_settings,
        seed_catalog_settings,
        seed_collections_policy_settings,
        seed_collections_settings,
        seed_comms_settings,
        seed_geocoding_settings,
        seed_gis_settings,
        seed_imports_settings,
        seed_network_monitoring_settings,
        seed_network_policy_settings,
        seed_network_settings,
        seed_notification_settings,
        seed_notification_templates,
        seed_provisioning_settings,
        seed_provisioning_workflows,
        seed_radius_policy_settings,
        seed_radius_settings,
        seed_scheduler_runtime_settings,
        seed_scheduler_settings,
        seed_subscriber_settings,
        seed_tr069_settings,
        seed_usage_policy_settings,
        seed_usage_settings,
        seed_wireguard_settings,
    )

    if settings.enforce_credential_encryption:
        require_encryption_key(enforce=True)
        logger.info(
            "Credential encryption enforcement enabled",
            extra={"event": "credential_encryption_enforced"},
        )

    _assert_required_schema()

    try:
        ensure_storage_bucket(raise_on_failure=False)
    except (ObjectStorageConnectionError, ObjectStorageError):
        logger.warning(
            "Storage bucket initialization deferred during startup",
            extra={"event": "storage_bucket_init_deferred"},
        )
    except Exception:
        logger.exception(
            "Failed to ensure storage bucket during startup",
            extra={"event": "storage_bucket_init_failed"},
        )
    db = SessionLocal()
    try:
        # Before every settings seed: settings are tenant-scoped (ADR-0009), so
        # the tenant they belong to must exist first. Idempotent — an existing
        # tenant is returned untouched, never reverted.
        from app.services.operator_tenant import provision_operator_tenant

        provision_operator_tenant(db)

        seed_auth_settings(db)
        seed_auth_policy_settings(db)
        seed_audit_settings(db)
        seed_billing_settings(db)
        seed_catalog_settings(db)
        seed_imports_settings(db)
        seed_gis_settings(db)
        seed_usage_settings(db)
        seed_usage_policy_settings(db)
        seed_notification_settings(db)
        seed_notification_templates(db)
        seed_collections_settings(db)
        seed_collections_policy_settings(db)
        seed_geocoding_settings(db)
        seed_radius_settings(db)
        from app.services.external_radius_targets import (
            seed_external_radius_target_from_env,
        )

        seed_external_radius_target_from_env(db)
        seed_radius_policy_settings(db)
        seed_scheduler_settings(db)
        seed_subscriber_settings(db)
        seed_provisioning_settings(db)
        seed_provisioning_workflows(db)
        seed_tr069_settings(db)
        seed_network_policy_settings(db)
        seed_network_settings(db)
        seed_network_monitoring_settings(db)
        seed_comms_settings(db)
        seed_wireguard_settings(db)
        seed_scheduler_runtime_settings(db)
    finally:
        db.close()
    logger.info(
        "startup_seed_complete",
        extra={
            "event": "startup_seed_complete",
            "duration_ms": round((monotonic() - started_at) * 1000.0, 2),
        },
    )


def _startup_preflight() -> None:
    """Fast, fail-fast checks that MUST pass before serving traffic: the boot
    secrets, credential encryption enforcement and required schema. The slow,
    idempotent default-settings seeding is deferred off the serving path — see
    [_run_deferred_startup]."""
    _check_test_environment_leakage()
    from app.config import settings
    from app.services.credential_crypto import require_encryption_key
    from app.services.kernel_key_provider import (
        install_if_configured as install_settings_keyring,
    )
    from app.services.kernel_secret_source import install_if_configured

    # BEFORE the encryption check below, which reads one of the secrets this
    # loads. Held here, once, and never fetched again: nothing on a request
    # path may reach OpenBao (starter ADR-0009). Fail-fast rather than
    # deferred, because a process without its keys is not a process that
    # should be serving — see `install_if_configured`.
    held = install_if_configured()
    logger.info(
        "boot_secrets_held",
        extra={"event": "boot_secrets_held", "names": list(held), "count": len(held)},
    )

    # The settings-encryption keyring, loaded on the same terms and for the
    # same reason: the keys are in memory from now on, so a store outage later
    # cannot reach a settings read. Ids only in the log — never material.
    key_ids = install_settings_keyring()
    logger.info(
        "settings_keyring_held",
        extra={
            "event": "settings_keyring_held",
            "key_ids": list(key_ids),
            "count": len(key_ids),
        },
    )

    if settings.enforce_credential_encryption:
        require_encryption_key(enforce=True)
        logger.info(
            "Credential encryption enforcement enabled",
            extra={"event": "credential_encryption_enforced"},
        )
    _assert_required_schema()


def _prewarm_admin_dashboard() -> None:
    from app.config import settings

    if not settings.dashboard_prewarm_enabled:
        return
    from app.services import web_admin_dashboard

    if not web_admin_dashboard.prewarm_dashboard_global_cache():
        raise RuntimeError("dashboard cache prewarm failed")


async def _run_deferred_startup() -> None:
    """Run slow, idempotent, non-fatal startup work in worker threads so it
    never blocks the event loop (single-worker safe) or delays serving.

    Default-settings seeding is ~100s of upserts against a busy DB; running it
    inline kept the app dead to health checks for minutes after every restart.
    The seeds are idempotent (upsert/skip-if-exists), so deferring is safe."""
    for fn, step in (
        (_prewarm_admin_dashboard, "dashboard_prewarm"),
        (_seed_startup_settings, "seed"),
        (_warn_on_scheduler_registry_drift, "scheduler_drift"),
    ):
        try:
            await asyncio.to_thread(fn)
        except Exception:
            logger.exception(
                "deferred_startup_step_failed",
                extra={"event": "deferred_startup_step_failed", "step": step},
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _DEFERRED_ROUTER_TASK, _DEFERRED_STARTUP_TASK
    logger.info("app_lifespan_start", extra={"event": "app_lifespan_start"})
    _log_release_metadata("api")
    # Cap the threadpool that runs sync request handlers so a worker never holds
    # more concurrent DB-touching threads than the connection pool can serve.
    try:
        from anyio import to_thread

        from app.config import settings as _settings

        limit = _settings.web_threadpool_limit
        if limit > 0:
            to_thread.current_default_thread_limiter().total_tokens = limit
            logger.info(
                "threadpool_limit_set",
                extra={"event": "threadpool_limit_set", "total_tokens": limit},
            )
    except Exception:
        logger.warning("Failed to set threadpool limit", exc_info=True)
    _startup_preflight()
    from app.websocket.manager import get_connection_manager

    manager = get_connection_manager()
    logger.info(
        "websocket_manager_connect_begin",
        extra={"event": "websocket_manager_connect_begin"},
    )
    await manager.connect()
    logger.info(
        "websocket_manager_connect_complete",
        extra={"event": "websocket_manager_connect_complete"},
    )
    # Defer slow, idempotent, non-fatal startup work (default-settings seeding,
    # integration health probes) off the serving path so a restart serves
    # health/traffic in seconds, not minutes.
    _DEFERRED_STARTUP_TASK = asyncio.create_task(_run_deferred_startup())
    _DEFERRED_ROUTER_TASK = asyncio.create_task(_load_deferred_api_routers(app))
    try:
        yield
    finally:
        for _task_name in ("_DEFERRED_STARTUP_TASK", "_DEFERRED_ROUTER_TASK"):
            _task = globals().get(_task_name)
            if _task is not None:
                _task.cancel()
                try:
                    await _task
                except asyncio.CancelledError:
                    pass
                globals()[_task_name] = None
        logger.info(
            "websocket_manager_disconnect_begin",
            extra={"event": "websocket_manager_disconnect_begin"},
        )
        await manager.disconnect()
        logger.info(
            "websocket_manager_disconnect_complete",
            extra={"event": "websocket_manager_disconnect_complete"},
        )


app = FastAPI(title="dotmac_sm API", lifespan=lifespan)
configure_logging()
setup_monitoring(
    app_name="dotmac-sub",
    server=os.getenv("SERVER_NAME", "default"),
)
setup_otel(app)
app.add_middleware(ObservabilityMiddleware)
register_error_handlers(app)
_include_core_routers(app)


@app.post("/api/v1/alerts/grafana-webhook", include_in_schema=False)
async def grafana_webhook_sink(request: Request) -> Response:
    """Accept Grafana alert webhooks even when alert ingestion is not configured."""
    try:
        await request.body()
    except Exception:
        logger.debug("Failed to read Grafana webhook body", exc_info=True)
    return Response(status_code=204)


def _default_audit_settings() -> dict:
    return {
        "enabled": True,
        "methods": {"POST", "PUT", "PATCH", "DELETE"},
        "skip_paths": ["/static", "/web", "/health"],
        "read_trigger_header": "x-audit-read",
        "read_trigger_query": "audit",
    }


def _get_cached_audit_settings(*, allow_stale: bool = False) -> dict | None:
    """Return cached audit settings when fresh, or stale when explicitly allowed."""
    with _AUDIT_SETTINGS_LOCK:
        if not _AUDIT_SETTINGS_CACHE or not _AUDIT_SETTINGS_CACHE_AT:
            return None
        if allow_stale or (
            monotonic() - _AUDIT_SETTINGS_CACHE_AT < _AUDIT_SETTINGS_CACHE_TTL_SECONDS
        ):
            return _AUDIT_SETTINGS_CACHE
    return None


def _set_audit_settings_refresh_timeout(db: Session) -> None:
    """Keep best-effort audit config refreshes from waiting behind DB pressure."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(
        text(f"SET LOCAL statement_timeout = '{_AUDIT_SETTINGS_REFRESH_TIMEOUT_MS}ms'")
    )


def _is_audit_always_skipped(path: str) -> bool:
    return path in _AUDIT_ALWAYS_SKIP_EXACT_PATHS or any(
        path.startswith(prefix) for prefix in _AUDIT_ALWAYS_SKIP_PATH_PREFIXES
    )


def _try_log_audit_request(request: Request, response: Response) -> None:
    db = SessionLocal()
    try:
        audit_service.audit_events.log_request(db, request, response)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "audit_log_request_failed",
            exc_info=True,
            extra={"event": "audit_log_request_failed"},
        )
    finally:
        db.close()


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    response: Response
    path = request.url.path
    if _is_audit_always_skipped(path):
        return await call_next(request)

    # Check cache first to avoid unnecessary session creation
    audit_settings = _get_cached_audit_settings()
    if audit_settings is None:
        audit_settings = _get_cached_audit_settings(allow_stale=True)
        if audit_settings is None:
            db = SessionLocal()
            try:
                audit_settings = _load_audit_settings(db)
            except SQLAlchemyError:
                logger.warning(
                    "audit_settings_refresh_failed",
                    exc_info=True,
                    extra={
                        "event": "audit_settings_refresh_failed",
                        "using_stale_cache": False,
                    },
                )
                audit_settings = _default_audit_settings()
                audit_settings["enabled"] = False
            finally:
                db.close()
    if not audit_settings["enabled"]:
        return await call_next(request)
    track_read = request.method == "GET" and (
        request.headers.get(audit_settings["read_trigger_header"], "").lower() == "true"
        or request.query_params.get(audit_settings["read_trigger_query"]) == "true"
    )
    should_log = request.method in audit_settings["methods"] or track_read
    if _is_audit_path_skipped(path, audit_settings["skip_paths"]):
        should_log = False
    try:
        response = await call_next(request)
    except Exception:
        if should_log:
            _try_log_audit_request(request, Response(status_code=500))
        raise
    if should_log:
        _try_log_audit_request(request, response)
    return response


# ---------------------------------------------------------------------------
# Domain-based portal routing
# ---------------------------------------------------------------------------
# Reads selfcare_domain from settings to redirect / → /portal/ on the
# selfcare host. Changes in the admin UI (System → Config → Customer Portal)
# take effect within 30 s (cache TTL).
class DomainRoutingCache(TypedDict):
    ts: float
    selfcare: str
    redirect: str


_domain_routing_cache: DomainRoutingCache = {
    "ts": 0.0,
    "selfcare": "",
    "redirect": "/portal/",
}


def _load_domain_routing(db: Session) -> dict[str, str]:
    """Return cached selfcare domain + redirect target."""
    now = monotonic()
    if now - _domain_routing_cache["ts"] < 30:
        return {
            "selfcare": _domain_routing_cache["selfcare"],
            "redirect": _domain_routing_cache["redirect"],
        }
    from sqlalchemy import select

    from app.models.domain_settings import DomainSetting, SettingDomain

    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.auth)
        .where(DomainSetting.key.in_(["selfcare_domain", "selfcare_redirect_root"]))
    )
    rows = {r.key: (r.value_text or "") for r in db.scalars(stmt).all()}
    _domain_routing_cache["selfcare"] = rows.get("selfcare_domain", "")
    _domain_routing_cache["redirect"] = rows.get("selfcare_redirect_root", "/portal/")
    _domain_routing_cache["ts"] = now
    return {
        "selfcare": _domain_routing_cache["selfcare"],
        "redirect": _domain_routing_cache["redirect"],
    }


def _get_cached_domain_routing(*, allow_stale: bool = False) -> dict[str, str] | None:
    """Return cached domain routing when still fresh, or stale if allowed."""
    cache_ts = _domain_routing_cache["ts"]
    if cache_ts <= 0:
        return None
    if allow_stale or monotonic() - cache_ts < 30:
        return {
            "selfcare": _domain_routing_cache["selfcare"],
            "redirect": _domain_routing_cache["redirect"],
        }
    return None


@app.middleware("http")
async def domain_routing_middleware(request: Request, call_next):
    """Apply lightweight host-aware routing for the selfcare domain."""
    path = request.url.path
    if path not in {"", "/"}:
        return await call_next(request)

    host = (request.headers.get("host") or "").split(":")[0].lower()
    if not host:
        return await call_next(request)

    # Check cache first to avoid unnecessary session creation
    routing = _get_cached_domain_routing()
    if routing is None:
        db = SessionLocal()
        try:
            routing = _load_domain_routing(db)
        except SQLAlchemyError:
            routing = _get_cached_domain_routing(allow_stale=True) or {
                "selfcare": "",
                "redirect": "/portal/",
            }
            logger.warning(
                "domain_routing_refresh_failed",
                exc_info=True,
                extra={"event": "domain_routing_refresh_failed"},
            )
        finally:
            db.close()

    selfcare = str(routing.get("selfcare", "")).strip().lower()
    if not selfcare or host != selfcare:
        return await call_next(request)

    # Keep the selfcare host convenient by redirecting only the bare root
    # to the configured portal landing page. All other paths stay reachable.
    redirect_target = str(routing.get("redirect", "/portal/"))
    from starlette.responses import RedirectResponse as StarletteRedirect

    return StarletteRedirect(url=redirect_target, status_code=302)


# CSRF Protection paths - protect all web portals and auth forms
_CSRF_PROTECTED_PATHS = [
    "/admin/",
    "/web/",
    "/portal/",
    "/reseller/",
    "/vendor/",
    "/auth/",
    "/lead-intake/",
]
_CSRF_EXEMPT_PATHS = ["/api/", "/health", "/metrics", "/static/"]
_CSRF_EXEMPT_EXACT_PATHS = {"/portal/auth/logout"}
_WEB_AUTH_REFRESH_PATHS = ("/admin/", "/web/")
_WEB_AUTH_REFRESH_EXEMPT_PATHS = (
    "/auth/",
    "/api/",
    "/health",
    "/metrics",
    "/static/",
)


def _rewrite_cookie_header(request: Request, name: str, value: str) -> None:
    """Make a refreshed cookie visible to downstream dependencies in this request."""
    cookies = dict(request.cookies)
    cookies[name] = value
    cookie = SimpleCookie()
    for key, cookie_value in cookies.items():
        cookie[key] = cookie_value
    header_value = "; ".join(
        f"{morsel.key}={morsel.value}" for morsel in cookie.values()
    ).encode("latin-1")
    headers = [
        (key, val)
        for key, val in request.scope.get("headers", [])
        if key.lower() != b"cookie"
    ]
    headers.append((b"cookie", header_value))
    request.scope["headers"] = headers
    if hasattr(request, "_cookies"):
        delattr(request, "_cookies")


def _web_auth_refresh_candidate(request: Request) -> bool:
    path = request.url.path
    if any(path.startswith(exempt) for exempt in _WEB_AUTH_REFRESH_EXEMPT_PATHS):
        return False
    return any(path.startswith(prefix) for prefix in _WEB_AUTH_REFRESH_PATHS)


async def _terminated_request_response(
    request: Request, method: str, path: str
) -> Response:
    disconnected = await request.is_disconnected()
    logger.info(
        "No response returned from downstream app; request terminated (%s): %s %s",
        "client_disconnected" if disconnected else "reload_or_shutdown",
        method,
        path,
    )
    return Response(status_code=204)


@app.middleware("http")
async def web_auth_refresh_middleware(request: Request, call_next):
    """Refresh expired web access cookies before protected routes handle the request."""
    refreshed: tuple[str, str | None] | None = None
    if _web_auth_refresh_candidate(request):
        db = SessionLocal()
        from app.web.auth.dependencies import validate_session_token

        try:
            auth_info = validate_session_token(request, db)
            if auth_info:
                request.state.auth = auth_info
                request.state.user = auth_info["subscriber"]
                request.state.actor_id = auth_info["subscriber_id"]
                request.state.actor_type = auth_info.get("principal_type", "user")
            else:
                from app.services import auth_flow as auth_flow_service
                from app.services.auth_flow import AuthFlow

                refresh_token = AuthFlow.resolve_refresh_token(request, None, db)
                if refresh_token:
                    result = auth_flow_service.auth_flow.refresh(
                        db, refresh_token, request
                    )
                    session_token = auth_flow_service.issue_web_session_token(
                        db, str(result.get("access_token", ""))
                    )
                    _rewrite_cookie_header(request, "session_token", session_token)
                    refreshed = (session_token, result.get("refresh_token"))
        except Exception:
            logger.debug("Web auth pre-route refresh failed", exc_info=True)
        finally:
            db.close()

    response = await call_next(request)
    if refreshed is not None:
        db = SessionLocal()
        try:
            from app.services.web_auth import (
                _is_https_request,
                _session_cookie_settings,
                _set_refresh_cookie,
            )

            session_token, refresh_token = refreshed
            cookie_cfg = _session_cookie_settings(db)
            response.set_cookie(
                key="session_token",
                value=session_token,
                httponly=True,
                secure=bool(cookie_cfg["secure"]) and _is_https_request(request),
                samesite=cookie_cfg["samesite"],
            )
            if refresh_token:
                _set_refresh_cookie(response, db, refresh_token, request)
        finally:
            db.close()
    return response


@app.middleware("http")
async def fiber_widget_cors_middleware(request: Request, call_next):
    """Expose only native widget routes to the exact fiber-site browser origin."""

    if not request.url.path.startswith("/widget"):
        return await call_next(request)

    from app.config import settings

    origin = str(request.headers.get("origin") or "").rstrip("/")
    allowed_origin = settings.fiber_chat_allowed_origin
    if request.method.upper() == "OPTIONS":
        if origin != allowed_origin:
            return Response(status_code=403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": allowed_origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Visitor-Token",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            },
        )

    response = await call_next(request)
    if origin == allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Visitor-Token"
        )
        response.headers.append("Vary", "Origin")
    return response


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """
    CSRF protection middleware using double-submit cookie pattern.

    For GET requests: Sets CSRF cookie if not present.
    For POST/PUT/DELETE on protected paths: Validates CSRF token.
    """
    path = request.url.path
    method = request.method.upper()

    # Skip CSRF for exempt paths
    if path in _CSRF_EXEMPT_EXACT_PATHS or any(
        path.startswith(exempt) for exempt in _CSRF_EXEMPT_PATHS
    ):
        try:
            return await call_next(request)
        except RuntimeError as exc:
            # Starlette BaseHTTPMiddleware quirk: client disconnect mid-request
            # (common with Docker healthchecks racing the response) makes the
            # inner ASGI task end without sending a response; `call_next` then
            # raises RuntimeError("No response returned."). For the trivial
            # /health endpoint the client has already gone, so synthesize an
            # OK to stop filling logs with stack traces.
            if path == "/health" and "No response returned" in str(exc):
                from starlette.responses import JSONResponse as _JSON

                return _JSON({"status": "ok"})
            raise

    # Check if path needs CSRF protection
    needs_protection = any(
        path.startswith(protected) for protected in _CSRF_PROTECTED_PATHS
    )

    if not needs_protection:
        return await call_next(request)

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    generated_token: str | None = None

    if not cookie_token:
        generated_token = generate_csrf_token()
        request.state.csrf_token = generated_token
    else:
        request.state.csrf_token = cookie_token

    # For state-changing methods, validate CSRF token
    if method in ("POST", "PUT", "DELETE", "PATCH"):
        from fastapi.responses import HTMLResponse

        def _csrf_forbidden(reason: str) -> HTMLResponse:
            logger.warning(
                "CSRF validation failed for %s %s: %s",
                method,
                path,
                reason,
            )
            # Generate a request ID for tracking
            import uuid

            request_id = str(uuid.uuid4())[:8]
            try:
                from jinja2 import Environment, FileSystemLoader

                env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
                template = env.get_template("errors/csrf.html")
                content = template.render(request_id=request_id)
                return HTMLResponse(content=content, status_code=403)
            except Exception:
                # Fallback to simple HTML if template rendering fails
                return HTMLResponse(
                    content=f"""<!DOCTYPE html>
<html><head><title>Session Expired</title></head>
<body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc">
<div style="text-align:center;max-width:400px;padding:20px">
<h1 style="color:#1e293b">Session Expired</h1>
<p style="color:#64748b">Your session has expired or the security token is invalid. Please refresh the page and try again.</p>
<p style="color:#94a3b8;font-size:12px">Reference: {request_id}</p>
<button onclick="location.reload()" style="margin-top:16px;padding:12px 24px;background:#2563eb;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600">Refresh Page</button>
</div></body></html>""",
                    status_code=403,
                )

        if not cookie_token:
            # No CSRF cookie - reject request
            return _csrf_forbidden(
                "CSRF token missing. Please refresh the page and try again."
            )

        # Check header first (for HTMX/fetch requests)
        header_token = request.headers.get(CSRF_HEADER_NAME)
        if header_token:
            if not secrets.compare_digest(cookie_token, header_token):
                return _csrf_forbidden(
                    "CSRF token invalid. Please refresh the page and try again."
                )
        else:
            # For form submissions, check form data
            content_type = request.headers.get("content-type", "")
            if (
                "application/x-www-form-urlencoded" in content_type
                or "multipart/form-data" in content_type
            ):
                # Read body and check token
                body = await request.body()

                # Parse form data to get CSRF token
                from urllib.parse import parse_qs

                form_token: str | None = None
                try:
                    if "multipart/form-data" in content_type:
                        # Parse multipart form data properly using email.parser
                        # Extract boundary from content-type header
                        import re
                        from email.parser import BytesParser
                        from email.policy import HTTP

                        boundary_match = re.search(r"boundary=([^\s;]+)", content_type)
                        if boundary_match:
                            boundary = boundary_match.group(1).strip('"')
                            # Construct a valid MIME message for parsing
                            mime_header = f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n"
                            mime_message = mime_header.encode("utf-8") + body

                            parser = BytesParser(policy=HTTP)
                            msg = parser.parsebytes(mime_message)

                            # Walk through all parts to find CSRF token field
                            if msg.is_multipart():
                                for part in msg.iter_parts():
                                    content_disp = part.get("Content-Disposition", "")
                                    if (
                                        'name="_csrf_token"' in content_disp
                                        or "name=_csrf_token" in content_disp
                                    ):
                                        payload = part.get_payload(decode=True)
                                        if isinstance(payload, (bytes, bytearray)):
                                            form_token = payload.decode(
                                                "utf-8", errors="ignore"
                                            ).strip()
                                            break
                                        if isinstance(payload, str) and payload.strip():
                                            form_token = payload.strip()
                                            break
                    else:
                        form_data = parse_qs(body.decode("utf-8"))
                        form_token = form_data.get("_csrf_token", [None])[0]

                    if not form_token:
                        return _csrf_forbidden(
                            "CSRF token missing. Please refresh the page and try again."
                        )
                    if not secrets.compare_digest(cookie_token, form_token):
                        return _csrf_forbidden(
                            "CSRF token invalid. Please refresh the page and try again."
                        )
                except Exception:
                    return _csrf_forbidden(
                        "CSRF token invalid. Please refresh the page and try again."
                    )

                # Reconstruct request with body for downstream handlers
                async def receive():
                    return {"type": "http.request", "body": body}

                request = Request(scope=request.scope, receive=receive)
            else:
                # Non-form state-changing requests must use header token.
                return _csrf_forbidden(
                    "CSRF token missing. Please refresh the page and try again."
                )

    try:
        response = await call_next(request)
    except RuntimeError as exc:
        if str(exc) == "No response returned.":
            # During client disconnects and dev auto-reload windows Starlette may not
            # produce a downstream response; treat it as a benign terminated request.
            return await _terminated_request_response(request, method, path)
        raise

    # Set CSRF cookie on responses if not present
    if generated_token:
        set_csrf_cookie(response, generated_token, request)

    return response


# ── Login rate limiting + security response headers ──────────────────────────
# Per-IP throttle on the login endpoints. The DB-backed per-account lockout
# stops repeated guesses against one account; this stops credential-stuffing
# that sprays a single attempt across many usernames from one source (the
# per-account lockout never trips for that pattern). In-memory per-worker, so
# the effective ceiling is roughly limit x worker-count — a brute-force brake,
# not a hard quota. Tune via env.
_LOGIN_RATE_LIMIT_PATHS = frozenset(
    {
        "/auth/login",
        "/portal/auth/login",
        "/reseller/auth/login",
        "/api/v1/auth/login",
    }
)


def _client_ip(request: Request) -> str:
    return client_ip(request)


_API_SYNC_PRESSURE_DEFAULT_PREFIXES = ("/api/v1/",)
_API_SYNC_PRESSURE_DEFAULT_EXEMPT_PREFIXES = (
    "/api/v1/auth/login",
    "/api/v1/alerts/",
    "/api/v1/webhooks/",
)
_API_SYNC_PRESSURE_DEFAULT_OFFENDER_IPS = ("149.102.158.167",)
_API_SYNC_FEED_PATHS = frozenset(
    {
        "/api/v1/billing-accounts/sync",
        "/api/v1/credit-notes/sync",
        "/api/v1/invoices/sync",
        "/api/v1/payment-channels/sync",
        "/api/v1/payments/sync",
        "/api/v1/resellers/sync",
        "/api/v1/subscribers/sync",
        "/api/v1/tax-rates/sync",
    }
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        logger.warning("Invalid integer env %s=%r; using %s", name, raw, default)
        return default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _path_matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


_READONLY_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Paths under /portal that must keep working in a read-only (view-as) session,
# above all exiting the impersonation back to the reseller portal.
_READONLY_EXEMPT_PREFIXES = ("/portal/auth/",)


@app.middleware("http")
async def view_as_readonly_middleware(request: Request, call_next):
    """Block state-changing requests in a read-only customer session.

    Reseller "view as customer" sessions are view-only (see what the customer
    sees, for support/diagnosis). Reseller actions on behalf of a customer go
    through the reseller portal's own attributed, audited routes — never by
    writing as the customer. Admin "Login as Customer" is not read-only.
    """
    if request.method.upper() in _READONLY_WRITE_METHODS:
        path = request.url.path
        if path.startswith("/portal/") and not any(
            path.startswith(p) for p in _READONLY_EXEMPT_PREFIXES
        ):
            from starlette.responses import JSONResponse as _JSONResponse

            from app.services import customer_portal

            token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
            session = customer_portal.get_customer_session(token) if token else None
            if session and session.get("read_only"):
                logger.info(
                    "Blocked read-only (view-as) write: %s %s", request.method, path
                )
                return _JSONResponse(
                    {
                        "detail": (
                            "This is a view-only session. Switch to your reseller "
                            "tools to act on this account."
                        )
                    },
                    status_code=403,
                )
    try:
        return await call_next(request)
    except RuntimeError as exc:
        if str(exc) == "No response returned.":
            return await _terminated_request_response(
                request, request.method.upper(), request.url.path
            )
        raise


@app.middleware("http")
async def login_rate_limit_middleware(request: Request, call_next):
    if request.method == "POST" and request.url.path in _LOGIN_RATE_LIMIT_PATHS:
        from starlette.responses import JSONResponse as _JSONResponse

        from app.services.rate_limiter_adapter import allow_operation

        limit = int(os.getenv("LOGIN_RATE_LIMIT_MAX", "20"))
        window = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))
        decision = allow_operation(
            f"login-ip:{request.url.path}:{_client_ip(request)}",
            limit=limit,
            window_seconds=window,
        )
        if not decision.allowed:
            retry_after = decision.retry_after_seconds or window
            return _JSONResponse(
                {"detail": "Too many login attempts. Please try again later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)


@app.middleware("http")
async def api_sync_pressure_guard_middleware(request: Request, call_next):
    """Throttle API sync bursts before downstream handlers can acquire DB sessions."""
    if not _env_bool("API_SYNC_PRESSURE_GUARD_ENABLED", True):
        return await call_next(request)

    path = request.url.path
    prefixes = _env_csv(
        "API_SYNC_PRESSURE_PATH_PREFIXES", _API_SYNC_PRESSURE_DEFAULT_PREFIXES
    )
    exempt_prefixes = _env_csv(
        "API_SYNC_PRESSURE_EXEMPT_PATH_PREFIXES",
        _API_SYNC_PRESSURE_DEFAULT_EXEMPT_PREFIXES,
    )
    if not _path_matches_prefix(path, prefixes) or _path_matches_prefix(
        path, exempt_prefixes
    ):
        return await call_next(request)

    from starlette.responses import JSONResponse as _JSONResponse

    from app.services.rate_limiter_adapter import allow_operation

    ip_address = _client_ip(request)
    window = _env_int("API_SYNC_PRESSURE_WINDOW_SECONDS", 60)
    offender_ips = set(
        _env_csv(
            "API_SYNC_PRESSURE_OFFENDER_IPS",
            _API_SYNC_PRESSURE_DEFAULT_OFFENDER_IPS,
        )
    )
    if path in _API_SYNC_FEED_PATHS:
        bucket = "feed"
        limit = _env_int("API_SYNC_PRESSURE_FEED_LIMIT", 60)
    elif ip_address in offender_ips:
        bucket = "listed"
        limit = _env_int("API_SYNC_PRESSURE_OFFENDER_LIMIT", 10)
    else:
        bucket = "general"
        limit = _env_int("API_SYNC_PRESSURE_PER_IP_LIMIT", 300)

    decision = allow_operation(
        f"api-v1-pressure:{bucket}:{ip_address}",
        limit=limit,
        window_seconds=window,
    )
    if decision.allowed:
        return await call_next(request)

    retry_after = decision.retry_after_seconds or window
    try:
        from app.metrics import API_SYNC_PRESSURE_LIMITED

        scope = next(
            (
                prefix
                for prefix in prefixes
                if path == prefix or path.startswith(prefix)
            ),
            "api",
        )
        API_SYNC_PRESSURE_LIMITED.labels(bucket, scope).inc()
    except Exception:
        logger.debug("api_sync_pressure_metric_failed", exc_info=True)
    return _JSONResponse(
        {"detail": "API sync pressure limit reached. Please retry shortly."},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Emit baseline security headers from the app itself, independent of the
    reverse proxy (the deployed proxy config can drift from the repo)."""
    try:
        response = await call_next(request)
    except RuntimeError as exc:
        if str(exc) == "No response returned.":
            return await _terminated_request_response(
                request, request.method.upper(), request.url.path
            )
        raise
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if _request_is_https(request):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains",
        )
    return response


def _load_audit_settings(db: Session):
    global _AUDIT_SETTINGS_CACHE, _AUDIT_SETTINGS_CACHE_AT
    now = monotonic()
    with _AUDIT_SETTINGS_LOCK:
        if (
            _AUDIT_SETTINGS_CACHE
            and _AUDIT_SETTINGS_CACHE_AT
            and now - _AUDIT_SETTINGS_CACHE_AT < _AUDIT_SETTINGS_CACHE_TTL_SECONDS
        ):
            return _AUDIT_SETTINGS_CACHE
    defaults = _default_audit_settings()
    _set_audit_settings_refresh_timeout(db)
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.audit)
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    values = {row.key: row for row in rows}
    if "enabled" in values:
        defaults["enabled"] = _to_bool(values["enabled"])
    if "methods" in values:
        defaults["methods"] = _to_list(values["methods"], upper=True)
    if "skip_paths" in values:
        defaults["skip_paths"] = _to_list(values["skip_paths"], upper=False)
    if "read_trigger_header" in values:
        defaults["read_trigger_header"] = _to_str(values["read_trigger_header"])
    if "read_trigger_query" in values:
        defaults["read_trigger_query"] = _to_str(values["read_trigger_query"])
    with _AUDIT_SETTINGS_LOCK:
        _AUDIT_SETTINGS_CACHE = defaults
        _AUDIT_SETTINGS_CACHE_AT = now
    return defaults


def _to_bool(setting: DomainSetting) -> bool:
    value = setting.value_json if setting.value_json is not None else setting.value_text
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _to_str(setting: DomainSetting) -> str:
    value = setting.value_text if setting.value_text is not None else setting.value_json
    if value is None:
        return ""
    return str(value)


def _to_list(setting: DomainSetting, upper: bool) -> set[str] | list[str]:
    value = setting.value_json if setting.value_json is not None else setting.value_text
    items: list[str]
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = []
    if upper:
        return {item.upper() for item in items}
    return items


def _is_audit_path_skipped(path: str, skip_paths: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in skip_paths)


def _include_api_router(router, dependencies=None):
    app.include_router(router, prefix="/api/v1", dependencies=dependencies)


# The namespaced package mount MUST precede Sub's catch-all /static mount.
# Serving the installed artifact directly keeps dotmac-ui the one writer of its
# own compiled CSS; no copy of it is checked into this repository.
app.mount(
    UI_ASSET_MOUNT,
    StaticFiles(directory=UI_ASSET_DIRECTORY),
    name="dotmac_ui_static",
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
