import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, TypeVar

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

if TYPE_CHECKING:
    from app.services.unit_of_work import UnitOfWork

T = TypeVar("T")


class Base(DeclarativeBase):
    pass


_LOCK_TIMEOUT_RE = re.compile(r"\d+(ms|s|min)?")


def resolve_migration_lock_timeout(raw: str | None = None) -> str:
    """Validated Postgres ``lock_timeout`` for the migration connection.

    Bounds how long a migration waits to ACQUIRE a lock (NOT statement
    runtime), so a schema-locking ``ALTER`` fails fast instead of queuing behind
    the live app and piling every subsequent query behind it. Defaults to
    ``5s``; override via ``ALEMBIC_LOCK_TIMEOUT`` (e.g. ``30s`` for a
    maintenance window, ``0`` to disable). Malformed input falls back to the
    default — the value is interpolated into a ``SET`` statement. The raw value
    is owned by ``settings.alembic_lock_timeout`` (the config owner reads
    ``ALEMBIC_LOCK_TIMEOUT``), not read here directly.
    """
    value = (raw if raw is not None else settings.alembic_lock_timeout).strip()
    return value if _LOCK_TIMEOUT_RE.fullmatch(value) else "5s"


def get_engine():
    connect_args = {}
    if settings.database_url.startswith(("postgresql://", "postgresql+")):
        server_options = (
            f"-c statement_timeout={settings.db_statement_timeout_ms} "
            f"-c lock_timeout={settings.db_lock_timeout_ms} "
            "-c idle_in_transaction_session_timeout="
            f"{settings.db_idle_in_transaction_session_timeout_ms}"
        )
        connect_args["options"] = server_options
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        connect_args=connect_args,
    )
    from app.services.db_error_observability import install_db_error_observability

    install_db_error_observability(engine)
    return engine


_engine = get_engine()


SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

# Register operator-tenant scope and transaction-span observation for every
# web/task/script session, including paths that never use the web dependency.
from app.services.session_hooks import install_session_hooks  # noqa: E402

install_session_hooks()


def dispose_engine() -> None:
    """Dispose pooled DB connections, especially after Celery prefork."""
    _engine.dispose()


def get_db():
    """Centralized database session dependency for FastAPI.

    Yields a database session and ensures it is closed after the request.
    Use this as a dependency in FastAPI route handlers.

    Example:
        @app.get("/items")
        def get_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def finish_read_transaction(db: Session) -> None:
    """Release a clean read transaction after response inputs are materialized."""
    if not db.in_transaction() or db.in_nested_transaction():
        return
    if db.new or db.dirty or db.deleted:
        return
    original_expire_on_commit = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = original_expire_on_commit


#: Both halves of the operator-report snapshot contract, requested as connection
#: execution options rather than issued as SQL.
#:
#: `SET TRANSACTION ISOLATION LEVEL ... READ ONLY` is only legal as the FIRST
#: statement in a transaction, and since the operator-tenant hook installs
#: `app.current_tenant` via set_config on `after_begin`, it never is: a statement
#: has always run before any caller SQL arrives. Every adapter that issued it
#: raised ActiveSqlTransaction on PostgreSQL while SQLite unit coverage stayed
#: green, because SQLite skips the branch entirely.
READ_ONLY_SNAPSHOT_OPTIONS: Final[Mapping[str, object]] = MappingProxyType(
    {
        "isolation_level": "REPEATABLE READ",
        "postgresql_readonly": True,
    }
)


#: The write counterpart. Deliberately a SEPARATE constant and seam: a
#: serializable writer and a read-only reporter share only the fact that both
#: were broken by the same regression. Reusing the read-only seam here would
#: hand a writer a `postgresql_readonly` connection and fail every write, and
#: widening the read-only seam to be write-capable would silently remove the
#: guarantee that a report cannot modify the database it measures.
SERIALIZABLE_WRITE_OPTIONS: Final[Mapping[str, object]] = MappingProxyType(
    {
        "isolation_level": "SERIALIZABLE",
        "postgresql_readonly": False,
    }
)


def begin_serializable_write(db: Session) -> None:
    """Pin an existing session's transaction to SERIALIZABLE, READ WRITE.

    Same defect as the read-only seam — `SET TRANSACTION ISOLATION LEVEL` is
    only legal as the first statement, and the operator-tenant hook always runs
    one first — but the opposite requirement: this caller must be able to write.

    Applied to a caller-owned session rather than yielding its own, because the
    backfill executes inside a command that owns its transaction.
    """

    if db.get_bind().dialect.name == "postgresql":
        db.connection(execution_options=dict(SERIALIZABLE_WRITE_OPTIONS))


def begin_read_only_snapshot(db: Session) -> None:
    """Pin an existing session's transaction to REPEATABLE READ, READ ONLY.

    The caller-owned counterpart to `read_only_snapshot_session`, for the report
    that cannot accept a yielded session because it owns its own savepoint
    discipline and layers further `SET LOCAL` rails on the same transaction.
    `SET LOCAL` stays legal mid-transaction; `SET TRANSACTION` is the one that
    must come first, which is exactly why it cannot be used here.
    """

    if db.get_bind().dialect.name == "postgresql":
        db.connection(execution_options=dict(READ_ONLY_SNAPSHOT_OPTIONS))


@contextmanager
def read_only_snapshot_session() -> Generator[Session, None, None]:
    """Yield a session pinned to one REPEATABLE READ, READ ONLY snapshot.

    The single seam every read-only operator report goes through. Two guarantees
    that must not be separated:

    - **REPEATABLE READ**, because a report assembled from several statements has
      to see one snapshot, or its own cohorts can disagree with each other.
    - **READ ONLY**, because a report that measures a production database must be
      structurally incapable of changing it.

    Fixing the isolation level while dropping read-only would leave a report that
    still looks correct and can now write, which is strictly worse than the
    failure it replaced.

    Non-PostgreSQL binds get an ordinary session: SQLite has no equivalent, and
    silently pretending otherwise is what hid the original defect.
    """

    db = SessionLocal()
    try:
        begin_read_only_snapshot(db)
        yield db
    finally:
        db.rollback()
        db.close()


def finish_read_response(db: Session, value: T) -> T:
    """Return an already-materialized read response after releasing its DB transaction."""
    finish_read_transaction(db)
    return value


@contextmanager
def form_write(db: Session) -> Generator[None, None, None]:
    """Legacy form-owned rollback guard; do not use for new or migrated code.

    Existing callers are tracked by the adapter-transaction shrink-only
    baseline. Migrate them to registered public command owners that finish the
    transaction before the form adapter maps an error.

    The legacy callers follow the shape::

        try:
            with form_write(db):
                service.create(...)        # may commit -> may raise
            return RedirectResponse(...)   # success
        except (IntegrityError, ValidationError, ValueError) as exc:
            # session is guaranteed clean here
            return render_form(db, error=...)   # re-queries the DB

    If the write aborts the transaction (an ``IntegrityError`` from a unique/FK
    constraint, a ``DataError`` from a numeric overflow / over-length value),
    the session is left in an aborted state. Any DB query the ``except`` block
    then runs to re-render the form (``get_sidebar_stats``, ``_base_context``,
    ``build_*_context`` …) would itself fail on the poisoned session, turning a
    recoverable 4xx into a 500.

    The wrapper rolls the session back before the exception reaches the
    handler's ``except``. Do not copy this route-owned transaction pattern;
    migrate the caller and its write service as one ownership slice.
    """
    try:
        yield
    except Exception:
        db.rollback()
        raise


@contextmanager
def task_session() -> Generator[Session, None, None]:
    """Legacy task-owned transaction helper; do not add new callers.

    Existing callers are migration debt. New and migrated tasks create and
    close a session while the registered command owner controls commit and
    rollback.

    The helper remains only until the tracked callers have migrated.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_uow() -> Generator["UnitOfWork", None, None]:
    """Legacy route-owned unit-of-work dependency; do not add new callers.

    The target contract separates adapter session lifecycle from transaction
    ownership. A registered public command owner controls the business
    transaction; routes only map transport inputs, outcomes, and errors.

    The dependency remains only for compatibility while its tracked callers
    migrate. It is not an example for new code.
    """
    from app.services.unit_of_work import UnitOfWork

    session = SessionLocal()
    try:
        uow = UnitOfWork(session, auto_commit=True)
        yield uow
    finally:
        session.close()
