"""Boundary guard for the `access.radius_projection` SOT owner.

`radcheck`/`radreply`/`radusergroup` (and the admin device-login
tables) decide whether a subscriber's session authenticates at the BNG. The
relationship map names `access.radius_projection` (app.services.radius_population)
as their single idempotent writer.

The migration debt is closed: no adapter may mutate these tables directly.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

# The canonical owner: app.services.radius_projection == radius_population.
OWNER = "app/services/radius_population.py"

# Shrink-only migration debt: scoped writers still to be collapsed into the
# owner. Remove an entry when its radcheck/radreply writes are gone. Adding an
# entry requires an explicit ownership decision — it means a new split-brain
# writer was introduced instead of requesting a projection.
WRITER_BASELINE: set[str] = set()

# Raw-psycopg literal SQL against the auth tables.
_RAW_SQL_WRITE = re.compile(
    r"(INSERT\s+INTO|DELETE\s+FROM|UPDATE)\s+\"?rad(check|reply|usergroup)(_admin)?\b",
    re.IGNORECASE,
)
# SQLAlchemy-Core writes against a reflected radcheck/radreply Table object.
_CORE_WRITE = re.compile(r"(insert|delete)\(\s*rad(check|reply|usergroup)(_table)?\b")


def _radius_auth_writers() -> set[str]:
    writers: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if _RAW_SQL_WRITE.search(src) or _CORE_WRITE.search(src):
            writers.add(str(path.relative_to(PROJECT_ROOT)))
    return writers


def test_owner_writes_the_radius_auth_tables() -> None:
    """The declared owner must actually be a writer, or the map is a lie."""
    assert OWNER in _radius_auth_writers(), (
        f"{OWNER} is declared access.radius_projection but no longer writes "
        "radcheck/radreply. Update the owner or the relationship map."
    )


def test_no_new_radcheck_radreply_writer() -> None:
    """Only the owner and the shrink-only baseline may write the auth tables."""
    writers = _radius_auth_writers()
    allowed = {OWNER} | WRITER_BASELINE
    new = sorted(writers - allowed)
    assert not new, (
        "new radcheck/radreply writer(s) outside access.radius_projection.\n"
        "Request a projection (full sweep or scoped reconcile) or enqueue "
        "refresh_radius_from_subs instead of writing the tables directly:\n  "
        + "\n  ".join(new)
    )


def test_writer_baseline_only_shrinks() -> None:
    """A baseline entry whose writes are gone must be removed from the list."""
    writers = _radius_auth_writers()
    stale = sorted(WRITER_BASELINE - writers)
    assert not stale, (
        "these modules no longer write radcheck/radreply — remove them from "
        "WRITER_BASELINE so the migration debt reflects reality:\n  "
        + "\n  ".join(stale)
    )


def test_concurrency_policy_stays_inside_the_projection_owner() -> None:
    owner_source = (PROJECT_ROOT / OWNER).read_text(encoding="utf-8")
    owner = service_relationship("access.radius_projection")
    relationship_map = (PROJECT_ROOT / "docs/SOT_RELATIONSHIP_MAP.md").read_text(
        encoding="utf-8"
    )

    assert "def _radcheck_policy_attrs(" in owner_source
    assert "simultaneous_use_enforcement_enabled" in owner_source
    assert "RADIUS simultaneous-session check/control placement and cutover" in (
        owner.owns
    )
    assert "Simultaneous-Use` is a FreeRADIUS check/control attribute" in (
        relationship_map
    )
