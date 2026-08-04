import os
import sys
from pathlib import Path

import pytest

from scripts.testing.host_test_policy import (
    HostTestPolicyError,
    enforce_pytest_host_policy,
)

# Refuse unsafe host/scope combinations before importing the application graph.
# This protects direct ``pytest`` invocations that bypass the Makefile owner.
try:
    enforce_pytest_host_policy(
        repo_root=Path(__file__).resolve().parents[1],
        argv=sys.argv[1:],
        environ=os.environ,
    )
except HostTestPolicyError as exc:
    raise pytest.UsageError(str(exc)) from exc

# The unit suite must never touch live infrastructure: app/config.py calls
# load_dotenv(), which may pull a deployment .env, and the Redis-backed
# SettingsCache could then read/write live settings keys — test writes like
# default_offer_status="inactive" poison the deployed cache
# TTL, and leak between test files, failing whole modules ("Offer is not
# active"). load_dotenv() does not override pre-set env vars, so stomp them
# before any app import. Port 9 refuses instantly, and the redis client's
# circuit breaker keeps subsequent lookups cheap.
os.environ["REDIS_URL"] = "redis://127.0.0.1:9/0"
os.environ["SESSION_REDIS_URL"] = "redis://127.0.0.1:9/0"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "false"
os.environ["GLITCHTIP_ENABLED"] = "false"
os.environ["GLITCHTIP_DSN"] = ""
os.environ["OTEL_ENABLED"] = "false"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
os.environ["OPENBAO_ADDR"] = ""
os.environ["OPENBAO_TOKEN"] = ""
os.environ["VAULT_ADDR"] = ""
os.environ["VAULT_TOKEN"] = ""
# Keep import-time globals such as Celery scheduler configuration from touching
# the deployment database URL loaded from .env. Tests that need a real database
# use TEST_DATABASE_URL or explicit SQLite engines below.
os.environ["DATABASE_URL"] = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:9/dotmac_sub_test"
    "?connect_timeout=1"
)
# Likewise the radacct importer: .env carries real FreeRADIUS DB credentials
# and the code has fallbacks; blank both so import_radius_accounting no-ops
# unless a test explicitly points it at a fixture database.
os.environ["RADIUS_DB_DSN"] = ""
os.environ["RADIUS_DB_HOST"] = ""
os.environ["RADIUS_SYNC_DB_URL"] = ""

import sqlite3
import uuid
from datetime import UTC

# Template filters/globals (app_datetime, presentations, brand) are attached by
# a Jinja2Templates __init__ patch that app.main installs at import. Tests that
# render templates with their own Jinja2Templates need the same patch.
from app.web.brand_globals import install_brand_jinja_global  # noqa: E402

install_brand_jinja_global()

from typing import Any

from geoalchemy2 import Geometry
from geoalchemy2.admin.dialects import sqlite as geoalchemy_sqlite_admin
from sqlalchemy import String, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base


class _JoseDateTimeProxy:
    def utcnow(self):
        from datetime import datetime

        return datetime.now(UTC)

    def now(self, tz: Any | None = None):
        from datetime import datetime

        return datetime.now(tz)

    def __getattr__(self, name: str) -> Any:
        from datetime import datetime

        return getattr(datetime, name)


@pytest.fixture(autouse=True)
def _patch_jose_datetime(monkeypatch):
    import jose.jwt as jose_jwt

    monkeypatch.setattr(jose_jwt, "datetime", _JoseDateTimeProxy(), raising=False)


@pytest.fixture(autouse=True)
def _disable_bundled_external_radius(monkeypatch, request):
    """Do not let unit tests read the deployment's legacy bootstrap DSN."""
    if request.node.path.name == "test_radius_dsn.py":
        return

    from app.services import radius as radius_service

    monkeypatch.setattr(radius_service.radius_dsn, "resolve_radius_dsn", lambda: None)


# Register UUID adapter for SQLite - store as string
sqlite3.register_adapter(uuid.UUID, lambda u: str(u))


class SQLiteUUID(TypeDecorator):
    """UUID type that works with SQLite by storing as string."""

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if isinstance(value, uuid.UUID):
                return str(value)
            return value
        return None

    def process_result_value(self, value, dialect):
        if value is not None:
            if not isinstance(value, uuid.UUID):
                return uuid.UUID(value)
            return value
        return None


# Monkey-patch SQLAlchemy's UUID type for SQLite compatibility
# This must happen before any models are imported
from sqlalchemy.sql import sqltypes

_original_uuid_bind_processor = sqltypes.Uuid.bind_processor
_original_uuid_result_processor = sqltypes.Uuid.result_processor


def _sqlite_uuid_bind_processor(self, dialect):
    if dialect.name == "sqlite":

        def process(value):
            if value is not None:
                if isinstance(value, uuid.UUID):
                    return str(value)
                return str(uuid.UUID(value)) if value else None
            return None

        return process
    return _original_uuid_bind_processor(self, dialect)


def _sqlite_uuid_result_processor(self, dialect, coltype):
    if dialect.name == "sqlite":

        def process(value):
            if value is not None:
                if isinstance(value, uuid.UUID):
                    return value
                return uuid.UUID(value) if value else None
            return None

        return process
    return _original_uuid_result_processor(self, dialect, coltype)


sqltypes.Uuid.bind_processor = _sqlite_uuid_bind_processor  # type: ignore[method-assign]
sqltypes.Uuid.result_processor = _sqlite_uuid_result_processor  # type: ignore[method-assign]


# Monkey-patch PostgreSQL JSONB type for SQLite compatibility
# SQLite uses JSON instead of JSONB

_original_jsonb_compile = None
_original_geometry_bind_expression = Geometry.bind_expression
_original_geometry_column_expression = Geometry.column_expression
_original_geometry_result_processor = Geometry.result_processor


def _patch_jsonb_for_sqlite():
    """Make JSONB compile as JSON for SQLite dialect."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_original_visit_JSONB"):
        # Store original if it exists, otherwise create a fallback
        if hasattr(SQLiteTypeCompiler, "visit_JSONB"):
            SQLiteTypeCompiler._original_visit_JSONB = SQLiteTypeCompiler.visit_JSONB

        def visit_JSONB(self, type_, **kw):
            return self.visit_JSON(type_, **kw)

        SQLiteTypeCompiler.visit_JSONB = visit_JSONB


_patch_jsonb_for_sqlite()


def _enable_sqlite_geometry_passthrough() -> None:
    """Avoid Spatialite-only wrappers when tests run on plain SQLite."""

    def _bind_expression(self, bindvalue):
        return bindvalue

    def _column_expression(self, col):
        return col

    def _result_processor(self, dialect, coltype):
        if dialect.name == "sqlite":
            return lambda value: value
        return _original_geometry_result_processor(self, dialect, coltype)

    Geometry.bind_expression = _bind_expression
    Geometry.column_expression = _column_expression
    Geometry.result_processor = _result_processor


def _restore_sqlite_geometry_passthrough() -> None:
    Geometry.bind_expression = _original_geometry_bind_expression
    Geometry.column_expression = _original_geometry_column_expression
    Geometry.result_processor = _original_geometry_result_processor


def _disable_sqlite_spatial_admin() -> None:
    """Avoid GeoAlchemy Spatialite admin calls when mod_spatialite is unavailable."""

    def _noop(*args, **kwargs):
        return None

    geoalchemy_sqlite_admin.after_create = _noop
    geoalchemy_sqlite_admin.before_drop = _noop


def _enable_sqlite_spatial_admin() -> None:
    """Restore default GeoAlchemy SQLite admin hooks."""
    import importlib

    importlib.reload(geoalchemy_sqlite_admin)


# Register the complete model graph before the shared schema fixture calls create_all.
import app.models  # noqa: F401
from app.models.catalog import AccessType, PriceBasis, RegionZone, ServiceType
from app.models.subscriber import Subscriber
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    SubscriptionCreate,
)
from app.schemas.gis import GeoLayerCreate
from app.schemas.network import OLTDeviceCreate
from app.schemas.network_monitoring import NetworkDeviceCreate, PopSiteCreate
from app.schemas.radius import RadiusServerCreate
from app.schemas.tr069 import Tr069AcsServerCreate
from app.services import catalog as catalog_service
from app.services import gis as gis_service
from app.services import network as network_service
from app.services import network_monitoring as network_monitoring_service
from app.services import radius as radius_service
from app.services import tr069 as tr069_service
from scripts.ci.migrated_test_database import (
    DatabaseContractError,
    parse_test_database_target,
    require_migrated_schema,
)


@pytest.fixture(scope="session")
def engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url:
        # PostgreSQL is the authoritative database-test lane.  Never repair or
        # synthesize it from current ORM metadata: doing so silently omits
        # migration-only constraints, indexes, triggers, defaults and history.
        target = parse_test_database_target(database_url)
        engine = create_engine(target.url)
        try:
            require_migrated_schema(engine)
        except DatabaseContractError:
            engine.dispose()
            raise
    else:
        # Fast non-authoritative unit lane.  SQLite/model metadata is useful for
        # service logic but is never integration or deployed-schema evidence.
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={
                "check_same_thread": False,
            },
            poolclass=StaticPool,
        )

        # pysqlite emits BEGIN lazily and runs SAVEPOINT/RELEASE in autocommit
        # mode, so releasing the outermost savepoint COMMITS the connection —
        # breaking Session.begin_nested() (used for per-item isolation, e.g.
        # zabbix_host_sync / uisp_sync) under the db_session fixture's
        # connection-level transaction. Standard SQLAlchemy recipe: take over
        # transaction control so BEGIN is emitted eagerly and savepoints nest
        # inside a real transaction.
        @event.listens_for(engine, "connect")
        def _sqlite_disable_implicit_begin(dbapi_connection, _connection_record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _sqlite_emit_begin(conn):
            conn.exec_driver_sql("BEGIN")

        @event.listens_for(engine, "connect")
        def _load_spatialite(dbapi_connection, _connection_record):
            dbapi_connection.enable_load_extension(True)
            try:
                dbapi_connection.load_extension("mod_spatialite")
                _enable_sqlite_spatial_admin()
                _restore_sqlite_geometry_passthrough()
            except Exception:
                _disable_sqlite_spatial_admin()
                _enable_sqlite_geometry_passthrough()
                pass  # Spatialite not available, some tests may fail
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            try:
                cursor.execute("SELECT InitSpatialMetaData(1)")
            except Exception:
                pass  # Already initialized or spatialite not available
            cursor.close()

        # Create a connection first to initialize spatialite
        with engine.connect() as conn:
            pass
        Base.metadata.create_all(engine)

    return engine


@pytest.fixture()
def db_session(engine):
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        # The application starts only after the one-time opening-balance
        # authority cutover. Unit tests therefore run as a fresh post-cutover
        # installation: accounts created by a test are native accounts with a
        # zero opening position plus their own events. Reconstruction tests
        # explicitly remove this empty bootstrap record when exercising the
        # migration cutover itself.
        from datetime import datetime
        from decimal import Decimal

        from app.models.prepaid_funding import PrepaidFundingReconstructionBatch

        bootstrap_at = datetime(2000, 1, 1, tzinfo=UTC)
        session.add(
            PrepaidFundingReconstructionBatch(
                manifest_sha256="0" * 64,
                manifest_payload_sha256="0" * 64,
                attestation_sha256="0" * 64,
                attestation_key_fingerprint_sha256="0" * 64,
                attestation_signed_at=bootstrap_at,
                blocker_manifest_sha256="0" * 64,
                candidate_cohort_sha256="0" * 64,
                source="pytest-empty-native-install-cutover",
                evidence_ref="pytest:native-install",
                position_at=bootstrap_at,
                currency="NGN",
                account_count=0,
                total_amount=Decimal("0.00"),
                approved_by="pytest",
                is_authority_cutover=True,
                approved_at=bootstrap_at,
            )
        )
        session.commit()
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def _unique_email() -> str:
    return f"test-{uuid.uuid4().hex}@example.com"


@pytest.fixture()
def subscriber(db_session):
    """Unified subscriber fixture - combines identity, account, and billing."""
    from app.services.subscriber import _default_reseller_id

    subscriber = Subscriber(
        first_name="Test",
        last_name="User",
        email=_unique_email(),
        # subscribers.reseller_id is NOT NULL on Postgres (migration 116). On
        # SQLite no House reseller is seeded so this resolves to None (the
        # column is nullable there); on the PG integration job it resolves to
        # House, keeping the shared fixture usable by flow tests.
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


# Legacy alias for backwards compatibility with tests expecting 'person'
@pytest.fixture()
def person(subscriber):
    """Alias for subscriber fixture for backward compatibility."""
    return subscriber


@pytest.fixture()
def subscriber_account(subscriber):
    """Legacy alias for unified Subscriber (formerly SubscriberAccount)."""
    return subscriber


@pytest.fixture()
def work_order():
    """Lightweight fixture for comms tests (legacy name for service order)."""
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4())


@pytest.fixture()
def ticket():
    """Lightweight fixture for comms tests."""
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4())


_TEST_FERNET_KEY = "IuBP8vzovIeLwFbxtGPHO8jMvZULQa6JUjuCNOp-Uec="


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", os.getenv("JWT_SECRET", "test-secret"))
    monkeypatch.setenv("JWT_ALGORITHM", os.getenv("JWT_ALGORITHM", "HS256"))
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY",
        os.getenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_FERNET_KEY),
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons after each test to prevent mock leaking."""
    yield
    # Reset event dispatcher to clear any mocked handlers
    try:
        from app.services.events.dispatcher import reset_dispatcher

        reset_dispatcher()
    except ImportError:
        pass
    # Clear the OpenBao secret cache. _fetch_secret_data is lru_cache'd with
    # id(httpx.get) as a cache-buster key; CPython recycles id() values across
    # GC'd monkeypatched functions, so a cached 200 payload from an earlier test
    # can leak into a later one and skip the live httpx call (e.g. a missing
    # secret then trips the field-not-found 500 branch instead of returning 404).
    try:
        from app.services import secrets

        secrets.clear_cache()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _isolate_root_logging():
    """Restore the root logger's handlers after each test.

    Several app modules mutate the *root* logger at import time:
    ``app.main`` calls ``configure_logging()`` and a handful of service /
    migration modules call ``logging.basicConfig(...)`` at module scope. Each
    of those attaches a ``StreamHandler`` bound to whatever ``sys.stdout`` /
    ``sys.stderr`` is live *at import time* — which, under pytest, is the
    capture stream of whichever test imported the module first. When pytest
    tears that capture down, the still-attached handler points at a closed
    stream, so every later test that logs through the root logger raises
    ``ValueError: I/O operation on closed file`` ("--- Logging error ---" in
    CI). That manifests as a broad, ordering-dependent cascade of failures in
    otherwise-unrelated tests that all pass in isolation.

    The handler itself is hardened at the source (see
    ``app.logging.StderrStreamHandler``); this fixture is the defence in depth
    that also neutralises the ``basicConfig`` import-time handlers, by
    snapshotting the root logger's handler list before each test and restoring
    it afterwards so no stale-stream handler can leak across test boundaries.
    """
    import logging

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers


@pytest.fixture()
def pop_site(db_session):
    """Point of Presence for network tests."""
    pop_site = network_monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(
            name="Test POP",
            code="POP001",
        ),
    )
    return pop_site


@pytest.fixture()
def network_device(db_session, pop_site):
    """Network device for monitoring tests."""
    device = network_monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Test Router",
            hostname="router-01.test.local",
            pop_site_id=pop_site.id,
        ),
    )
    return device


@pytest.fixture()
def olt_device(db_session):
    """OLT device for fiber tests."""
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(
            name="Test OLT",
            hostname="olt-01.test.local",
        ),
    )
    return olt


@pytest.fixture()
def catalog_offer(db_session):
    """Catalog offer for subscription tests."""
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Standard Internet",
            code="STD-INT",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    # Create offer version linking to offer
    catalog_service.offer_versions.create(
        db_session,
        OfferVersionCreate(
            offer_id=offer.id,
            version_number=1,
            name="Standard Internet v1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    return offer


@pytest.fixture()
def subscription(db_session, subscriber, catalog_offer):
    """Active subscription for usage tests.

    Defaults to POSTPAID (the invoice-eligible mode). The model column default
    is prepaid, but generic invoice-generation tests use postpaid by default.
    Prepaid monthly-invoicing tests override billing_mode explicitly.
    """
    from app.models.catalog import BillingMode

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()
    return subscription


@pytest.fixture()
def radius_server(db_session):
    """RADIUS server for auth tests."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Test RADIUS",
            host="radius.test.local",
        ),
    )
    return server


@pytest.fixture()
def acs_server(db_session):
    """TR-069 ACS server for CPE tests."""
    server = tr069_service.acs_servers.create(
        db_session,
        Tr069AcsServerCreate(
            name="Test ACS",
            cwmp_url="https://acs.test.local/cwmp",
            cwmp_username="acs-user",
            cwmp_password="acs-pass",
            connection_request_username="cr-user",
            connection_request_password="cr-pass",
            base_url="https://acs.test.local",
        ),
    )
    return server


@pytest.fixture()
def geo_layer(db_session):
    """GIS layer for geo tests."""
    layer = gis_service.geo_layers.create(
        db_session,
        GeoLayerCreate(
            name="Test Layer",
            layer_type="boundary",
        ),
    )
    return layer


# ============================================================================
# Network Fixtures
# ============================================================================


@pytest.fixture()
def region(db_session):
    """RegionZone for VLAN tests."""
    rz = RegionZone(
        name="Test Region",
        code="TEST",
    )
    db_session.add(rz)
    db_session.commit()
    db_session.refresh(rz)
    return rz


# Skip test modules outside the active regression suite for this branch.
collect_ignore_glob = [
    "test_admin_actor_ids.py",
    "test_customer_location_requests.py",
    "test_log_regressions.py",
    "test_transactional_audit_events.py",
    "test_web_system_audit_service.py",
]
