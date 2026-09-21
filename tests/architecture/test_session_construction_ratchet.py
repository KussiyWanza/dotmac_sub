"""Runtime-readiness inventory: a two-directional ratchet on local
engine/session construction outside `app.db`'s canonical `SessionLocal`.

This is characterization, not a cutover guard: Sub still owns its own
database/session authority (`app/db.py`), and nothing here adopts
`dotmac_kernel.session_runtime.DatabaseRuntime` or changes a pinned version.
Its only job is to make sure the inventory this ratchet backs
(`docs/sub-runtime-readiness-inventory.md`) cannot silently drift — a new
construction site the Kernel runtime-composition seam would need to account
for must be a reviewed, deliberate baseline edit, not an unnoticed diff.

Mirrors the existing `test_adapter_keyword_service_calls.py` two-directional
shape: grow-or-appear fails immediately; shrink-without-lowering-the-baseline
also fails, so the debt figure stays trustworthy in both directions.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from tests.architecture.session_construction_inventory import (
    TEST_ROOTS,
    construction_sites,
    counts_by_file,
    historical_residue_paths,
    production_counts_by_file,
    production_residue_counts_by_file,
    raw_production_counts_by_file,
    test_fixture_total_count,
)

BASELINE = Path("tests/architecture/session_construction_baseline.txt")

#: Aggregate, informational count of construction sites under `tests/`.
#: Deliberately NOT a per-file baseline: test fixtures legitimately construct
#: one ad hoc engine per test, and per-file churn there says nothing about
#: runtime readiness. This total is still two-directional so a wholesale
#: change in how tests touch the database (e.g. every fixture switching to a
#: new async pattern) cannot pass unnoticed either.
#: +2 from tests/integration/test_integration_inbox_concurrency.py: two new
#: two-session Postgres concurrency tests for the payment-inbox lease
#: (reclaim-vs-reclaim and reclaim-vs-completion), each with its own
#: `sessionmaker`, mirroring the file's existing test fixture pattern.
#: +2 from tests/integration/test_auth_session_refresh_concurrency.py: the
#: setup/verification factory and two concurrent owner-command sessions.
#: +1 from tests/integration/test_tr069_active_cpe_identity_concurrency.py:
#: a concurrent-write proof for migration 595_active_cpe_identity's new
#: partial unique index (two sessions racing to activate the same
#: cpe_device_id), reviewed test-fixture authorship for the CPE identity
#: cardinality program.
#: +1 from tests/integration/test_prepaid_funding_trigger_execution_rollback.py:
#: a real-Postgres two-connection proof that a forced failure in the
#: funding-consequence owner's own transaction rolls back only that
#: transaction, following the established two-real-connection pattern.
#: +2 from tests/integration/test_prepaid_renewal_nightly_isolation.py: two
#: real-Postgres tests (ambiguous-account isolation, unclassified-failure
#: abort) each with their own `sessionmaker`, driving the single-owner
#: funding-consequence fix through the real nightly entry point.
#: +1 from tests/integration/test_prepaid_account_lock_compatibility.py: a
#: deterministic two-session PostgreSQL proof that account-writer
#: serialization remains compatible with the prepaid review item's subscriber
#: foreign key.
#: +1 from tests/integration/test_prepaid_renewal_nightly_isolation.py: the
#: teardown session that relinquishes the test-created global authority-cutover
#: marker after independently committed nightly-owner transactions.
#: +2 from tests/test_eg8145v5_wifi_capability_migration.py: isolated in-memory
#: migration rehearsals for repairing and seeding the model capability each own
#: the disposable engine that Alembic Operations executes against.
#: +1 from tests/test_erp_staff_talk_mapping_permission_migration.py: one
#: isolated in-memory rehearsal proves the permission migration is idempotent
#: and reversible without sharing state with the application test database.
#: +2 from tests/integration/test_team_inbox_contact_link_concurrency.py: two
#: PostgreSQL concurrency proofs for reviewed endpoint links and first-inbound
#: identity claims, each with its own `sessionmaker`.
#: +1 from tests/integration/test_invoice_accounting_sync_keyset_postgres.py: a
#: real dual-session PostgreSQL proof that the accounting-sync feed's keyset
#: cursor does not skip a stationary row under a genuinely concurrent,
#: independently-committing writer, with its own `sessionmaker`.
TEST_FIXTURE_BASELINE_TOTAL = 132


def _baseline() -> dict[str, int]:
    allowed: dict[str, int] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, count = line.rpartition(" ")
        allowed[path] = int(count)
    return allowed


def test_no_new_local_session_construction_in_production_surfaces() -> None:
    allowed = _baseline()
    current = production_counts_by_file()

    new_files = sorted(set(current) - set(allowed))
    assert not new_files, (
        "These files construct a SQLAlchemy Engine or Session outside "
        "app.db's canonical SessionLocal factory, and are not in the "
        "runtime-readiness inventory baseline. Either route through "
        "app.db.SessionLocal, or add the site to "
        "tests/architecture/session_construction_baseline.txt and describe "
        "it in docs/sub-runtime-readiness-inventory.md — a shared Kernel "
        f"DatabaseRuntime would need to account for it: {new_files}"
    )

    grew = sorted(
        f"{path}: {count} > {allowed[path]} allowed"
        for path, count in current.items()
        if count > allowed[path]
    )
    assert not grew, (
        "New local engine/session construction was added to a file already "
        "carrying this debt. Update the baseline and the inventory doc "
        f"deliberately if the new site is intended: {grew}"
    )


def test_session_construction_baseline_has_no_stale_entries() -> None:
    """A file that no longer constructs its own engine/session must leave the
    baseline, so a retired site cannot silently reappear under cover of an
    old allowance."""

    current = production_counts_by_file()
    stale = sorted(
        f"{path} (recorded {count}, now {current.get(path, 0)})"
        for path, count in _baseline().items()
        if current.get(path, 0) < count
    )
    assert not stale, (
        "These baseline counts are higher than reality — lower or remove "
        f"them so the ratchet keeps its grip: {stale}"
    )


def test_test_fixture_engine_family_is_swept() -> None:
    """`tests/` is a real entry-point family (fixtures, migration rehearsals,
    playwright harnesses) and must stay in the sweep, even though it is not
    held to the same per-file baseline as production code."""

    total = test_fixture_total_count()
    assert total <= TEST_FIXTURE_BASELINE_TOTAL, (
        f"tests/ now constructs {total} local engines/sessions, more than the "
        f"recorded {TEST_FIXTURE_BASELINE_TOTAL}. Raise "
        "TEST_FIXTURE_BASELINE_TOTAL deliberately if this growth is expected "
        "test-fixture authorship, not an unreviewed new pattern."
    )
    assert total >= TEST_FIXTURE_BASELINE_TOTAL, (
        f"tests/ now constructs only {total} local engines/sessions, fewer "
        f"than the recorded {TEST_FIXTURE_BASELINE_TOTAL}. Lower "
        "TEST_FIXTURE_BASELINE_TOTAL so the count stays trustworthy, or "
        "confirm no fixtures were silently dropped."
    )
    # `tests/` is exactly what TEST_ROOTS names — this pins the family this
    # test sweeps, so a future rename doesn't quietly stop scanning it.
    assert TEST_ROOTS == ("tests",)


def test_historical_residue_is_excluded_from_the_live_ratchet_but_still_swept() -> None:
    """Sensitivity proof (plant + near-miss, on the real repository): the
    LIVE production baseline excludes historical residue from a fully
    decommissioned integration, but the exclusion is not vacuous — the raw,
    unfiltered sweep still finds every one of those files, and a genuinely
    live file with real construction sites is never swept up by mistake.

    This is the residue-exclusion mechanism's own sensitivity proof: if the
    ledger it defers to changed shape and the exclusion silently stopped
    matching anything, `residue` below would be empty and this test would
    catch it immediately, rather than letting retirement residue quietly
    reappear as an unaccounted "new file" in the live ratchet.
    """

    residue = historical_residue_paths()
    raw = raw_production_counts_by_file()
    live = production_counts_by_file()
    residue_counts = production_residue_counts_by_file()

    # Plant: at least one production construction site is real residue, so
    # the exclusion has something to do — it is not exercising an empty set.
    residue_sites_found = {path for path in raw if path in residue}
    assert residue_sites_found, (
        "No production construction site currently matches the decommissioned "
        "integration's frozen surface. If that surface has genuinely shrunk "
        "to zero for production code, this assertion should be updated "
        "deliberately — but an empty set here means the exclusion mechanism "
        "cannot be proven to do anything."
    )

    # The residue set must be excluded from the LIVE view...
    assert not (residue_sites_found & set(live)), (
        f"These historical-residue files leaked into the LIVE production "
        f"baseline view: {sorted(residue_sites_found & set(live))}"
    )
    # ...but still accounted for, separately, in the residue view.
    assert residue_sites_found <= set(residue_counts)

    # Near-miss: a real, currently-live file with its own construction site
    # (unrelated to the decommissioned integration) must NOT be swept into
    # the residue exclusion by accident.
    assert "app/db.py" in live
    assert "app/db.py" not in residue_counts
    assert "app/db.py" not in residue

    # raw == live ∪ residue, with no overlap — the partition is exhaustive.
    assert set(raw) == set(live) | set(residue_counts)
    assert not (set(live) & set(residue_counts))
    assert sum(raw.values()) == sum(live.values()) + sum(residue_counts.values())


def _sites_in(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(textwrap.dedent(source))
    return construction_sites(tree)


def test_scanner_detects_a_planted_direct_engine_construction() -> None:
    """Sensitivity proof (plant): a real, unauthorized call site is named."""

    planted = """
        from sqlalchemy import create_engine

        def get_rogue_engine():
            return create_engine("postgresql://example/rogue")
    """
    sites = _sites_in(planted)
    assert sites == [(5, "create_engine")], (
        "The scanner must name the planted create_engine(...) call site by "
        f"line and canonical constructor name; got {sites}"
    )


def test_scanner_detects_a_planted_bound_session_construction() -> None:
    """Sensitivity proof (plant): a bound ORM Session(...) call is named,
    the same shape as app/services/db_session_adapter.py's advisory-lock
    session."""

    planted = """
        from sqlalchemy.orm import Session

        def rogue_session(conn):
            return Session(bind=conn, autoflush=False)
    """
    sites = _sites_in(planted)
    assert sites == [(5, "Session")]


def test_scanner_ignores_comment_and_docstring_mentions() -> None:
    """Sensitivity proof (near-miss): prose that names these symbols, with no
    actual call, must not be flagged."""

    near_miss = '''
        """This module used to call sessionmaker() directly; see app.db for
        the real create_engine() call and Session construction now."""

        # TODO: stop hand-rolling a create_engine call here.
        SESSIONMAKER_DOC = "sessionmaker"
    '''
    assert _sites_in(near_miss) == []


def test_scanner_ignores_an_unrelated_class_named_session() -> None:
    """Sensitivity proof (near-miss): a domain model class named `Session`
    that has nothing to do with SQLAlchemy session construction — the exact
    real-repository shape at app/models/auth.py's `class Session(Base):`."""

    near_miss = """
        from app.db import Base

        class Session(Base):
            __tablename__ = "sessions"
    """
    assert _sites_in(near_miss) == []


def test_scanner_ignores_bare_session_call_without_a_sqlalchemy_import() -> None:
    """Sensitivity proof (near-miss): a bare `Session(...)` call where
    `Session` was never imported from a `sqlalchemy*` module is not this
    module's construction primitive — it must not be assumed to be one."""

    near_miss = """
        from app.models.auth import Session

        def touch(existing_row_id):
            return Session(id=existing_row_id)
    """
    assert _sites_in(near_miss) == []


def test_real_repository_near_miss_stays_unflagged() -> None:
    """The actual `class Session(Base):` in app/models/auth.py must not be
    counted — proof against the real file, not only a synthetic stand-in."""

    counts = counts_by_file(("app/models",))
    assert "app/models/auth.py" not in counts, (
        "app/models/auth.py should only ever appear in this inventory if it "
        "gains a REAL SQLAlchemy engine/session construction call, not "
        "because of its unrelated `class Session(Base):` domain model."
    )
