"""Performance-regression tests for the native portal read paths (perf PR).

Covers the two hot-path fixes shipped alongside migration 251:

* **H1** — ``read_for_subscriber`` must resolve the whole quote set's install
  ``project_id`` in ONE query over the typed ``Project.quote_id`` relationship.
  Also asserts the batch resolver returns the same
  quote→project mapping the single-quote helper does (no-project and unique
  one-project parity).
* **H2** — ``_nearest_fiber_access_point`` orders by the KNN ``<->`` operator on
  the RAW 4326 geom (so the GiST index is usable), not by an
  ``ST_Transform(geom, 3857)`` wrap that forced a full scan. The metre distance
  value is still computed via the same 3857 transform on the winning row.

The KNN *execution* test needs a PostGIS backend (the default sqlite harness
has no spatial functions), so it skips off Postgres; the query-shape test is
backend-independent (it inspects the compiled SQL).
"""

import uuid

import pytest
from sqlalchemy import event

from app.models.network import FiberAccessPoint
from app.models.project import Project
from app.models.sales import Quote
from app.models.subscriber import Subscriber
from app.services.sales import selfserve


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="Ada",
        last_name="Obi",
        email=f"perf-{uuid.uuid4().hex[:10]}@example.com",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _quote(db, sub, **meta) -> Quote:
    quote = Quote(
        subscriber_id=sub.id,
        status="draft",
        currency="NGN",
        metadata_=meta or None,
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


def _project(db, sub, quote_id, *, is_active=True) -> Project:
    project = Project(
        name="Fiber install",
        subscriber_id=sub.id,
        quote_id=quote_id,
        status="open",
        is_active=is_active,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


class _StatementCounter:
    """Capture executed SQL so we can count project-resolution statements."""

    def __init__(self, engine):
        self._engine = engine
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    def matching(self, *needles: str) -> list[str]:
        lowered = [(s, s.lower()) for s in self.statements]
        return [s for s, low in lowered if all(n in low for n in needles)]


# ---------------------------------------------------------------------------
# H1 — batched project resolution
# ---------------------------------------------------------------------------


def test_read_for_subscriber_resolves_projects_in_one_query(db_session):
    """N quotes (each with an install project) must trigger exactly ONE
    project-resolution query, not one per quote (the N+1 that H1 kills)."""
    sub = _subscriber(db_session)
    for _ in range(4):
        q = _quote(db_session, sub, deposit_percent=50)
        _project(db_session, sub, q.id)

    with _StatementCounter(db_session.bind.engine) as counter:
        result = selfserve.selfserve_quotes.read_for_subscriber(db_session, str(sub.id))

    assert result["total"] == 4
    # The batch resolver is the only statement that scans projects by the
    # typed quote_id key. Exactly one, independent of the quote count.
    project_lookups = counter.matching("from projects", "quote_id")
    assert len(project_lookups) == 1, project_lookups
    # Every payload got its project id resolved (proves batch results applied).
    assert all(item["project_id"] is not None for item in result["quotes"])


def test_batch_resolver_parity_no_project(db_session):
    sub = _subscriber(db_session)
    q = _quote(db_session, sub)

    batch = selfserve._find_project_ids_for_quotes(db_session, [q.id])
    single = selfserve._find_project_id_for_quote(db_session, q.id)

    assert batch.get(str(q.id)) is None
    assert single is None


def test_batch_resolver_parity_one_project(db_session):
    sub = _subscriber(db_session)
    q = _quote(db_session, sub)
    project = _project(db_session, sub, q.id)

    batch = selfserve._find_project_ids_for_quotes(db_session, [q.id])
    single = selfserve._find_project_id_for_quote(db_session, q.id)

    assert batch[str(q.id)] == str(project.id)
    assert single == str(project.id)
    # Inactive projects are excluded (partial-index / is_active filter parity).
    project.is_active = False
    db_session.commit()
    assert selfserve._find_project_id_for_quote(db_session, q.id) is None


def test_batch_resolver_uses_unique_typed_quote_relationship(db_session):
    """The batch and single resolvers agree on the unique typed relationship."""
    sub = _subscriber(db_session)
    q = _quote(db_session, sub)
    project = _project(db_session, sub, q.id)

    batch = selfserve._find_project_ids_for_quotes(db_session, [q.id])
    single = selfserve._find_project_id_for_quote(db_session, q.id)

    assert batch[str(q.id)] == single
    assert single == str(project.id)


def test_batch_resolver_maps_mixed_set(db_session):
    """A mixed set (some quotes with projects, some without) maps correctly and
    omits quotes with no project."""
    sub = _subscriber(db_session)
    q_with = _quote(db_session, sub)
    q_without = _quote(db_session, sub)
    project = _project(db_session, sub, q_with.id)

    mapping = selfserve._find_project_ids_for_quotes(
        db_session, [q_with.id, q_without.id]
    )
    assert mapping == {str(q_with.id): str(project.id)}


# ---------------------------------------------------------------------------
# H2 — KNN nearest fiber access point
# ---------------------------------------------------------------------------


def test_nearest_fap_orders_by_knn_on_raw_geom(db_session):
    """The proximity query must ORDER BY the ``<->`` KNN operator applied to the
    raw geom (index-usable), not by an ``ST_Transform(geom, 3857)`` wrap — while
    still computing the metre distance via the 3857 transform in the SELECT."""
    engine = db_session.bind.engine
    captured: list[str] = []

    def _on(conn, cursor, statement, params, context, executemany):
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _on)
    try:
        # sqlite has no ST_* functions: execution raises, but the compiled SQL
        # is captured by the event before the driver rejects it.
        selfserve._nearest_fiber_access_point(db_session, 9.0, 7.4)
    except Exception:
        pass
    finally:
        event.remove(engine, "before_cursor_execute", _on)

    fap_sql = [s for s in captured if "fiber_access_points" in s.lower()]
    assert fap_sql, "proximity query was not issued"
    sql = fap_sql[-1].lower()
    order_by = sql.split("order by", 1)[1]
    # KNN operator drives the ordering on the raw geom column…
    assert "<->" in order_by
    # …and the ordering is NOT the old ST_Transform-wrapped distance.
    assert "st_transform" not in order_by
    # The metre distance is still computed via the 3857 transform (SELECT list).
    assert "st_transform" in sql


def test_nearest_fap_execution_returns_nearest(db_session):
    """Seed a few FAPs and confirm the KNN rewrite still returns the true
    nearest point + a sane metre distance. Requires PostGIS."""
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("nearest-FAP execution needs a PostGIS backend")

    from geoalchemy2.elements import WKTElement

    near = FiberAccessPoint(
        name="NAP-near",
        is_active=True,
        geom=WKTElement("POINT(7.400 9.000)", srid=4326),
    )
    mid = FiberAccessPoint(
        name="NAP-mid", is_active=True, geom=WKTElement("POINT(7.420 9.020)", srid=4326)
    )
    far = FiberAccessPoint(
        name="NAP-far", is_active=True, geom=WKTElement("POINT(7.600 9.200)", srid=4326)
    )
    db_session.add_all([far, mid, near])
    db_session.commit()

    fap, distance = selfserve._nearest_fiber_access_point(db_session, 9.001, 7.401)
    assert fap is not None
    assert fap.name == "NAP-near"
    # ~0.001° ≈ 150 m at this latitude; certainly well under a kilometre.
    assert 0.0 < distance < 1000.0
