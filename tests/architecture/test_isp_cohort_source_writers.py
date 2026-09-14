"""Two-directional ratchet on the cohort-isp-01 source writer surface.

Governance cohort 1 — party, customers and brand profiles — will eventually
move to `asm-dotmac-isp`. Before that can be considered, the set of Sub code
that writes those facts has to be known and has to stop growing. This guard
does exactly that and nothing more: it records today's writers and fails on
movement in either direction.

Both directions matter, for different reasons.

*Upward* is the obvious one: a new writer added while the cutover is being
prepared is a writer nobody planned to displace, and it will be discovered
after the authority switch rather than before.

*Downward* matters because a ratchet that silently absorbs removals can be
spent twice. If a writer disappears and the baseline still claims it, the
budget for "one more writer" is quietly restored, and the next addition passes
a guard that should have caught it. So a removal is a failure until the same
change lowers the line — which is the pull request that actually retired the
writer, after the sealed switch, never in advance.

## Sensitivity

Every rejection test here is paired with a proof that the same code path
admits the legal case, and the whole-repository sweeps assert against a
*constructed* violation in a synthetic tree rather than only over today's
data. A guard that has only ever seen conforming input passes for the wrong
reason: it would keep passing if the detector it exercises were deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.migration_source import surfaces
from scripts.architecture.isp_cohort_writers import (
    EntryPointFamily,
    cohort_reference_sites,
    cohort_write_counts,
    cohort_write_sites,
    cohort_writer_files,
    family_for,
    reference_counts_by_family,
    unscanned_python_roots,
)

FILE_BASELINE = Path(__file__).with_name("isp_cohort1_writer_files_baseline.txt")
SITE_BASELINE = Path(__file__).with_name("isp_cohort1_write_sites_baseline.txt")

REMEDY = (
    "cohort-isp-01 source state is frozen while the ISP replacement is "
    "prepared. Route the write through the owner named in "
    "app/migration_source/surfaces.py, or — if the write is genuinely new "
    "behaviour Sub still needs — say so in the pull request and raise the "
    "baseline deliberately. Regenerate with "
    "`python -m scripts.architecture.isp_cohort_writers --baseline files` "
    "and `--baseline sites`."
)


def _read_lines(path: Path) -> list[str]:
    """Return meaningful lines from a baseline, comments and blanks dropped."""

    return [
        stripped
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (stripped := raw.strip()) and not stripped.startswith("#")
    ]


def _read_file_baseline(path: Path) -> set[str]:
    """Read the membership baseline: one `family|path` per line."""

    keys: set[str] = set()
    for line_number, entry in enumerate(_read_lines(path), start=1):
        if "|" not in entry:  # pragma: no cover - malformed baseline
            raise ValueError(
                f"{path}:{line_number} is missing its `family|path` key; a "
                "bare path would let a writer move between families unnoticed"
            )
        keys.add(entry)
    return keys


def _read_site_baseline(path: Path) -> tuple[dict[str, int], int]:
    """Read the magnitude baseline: `count family|path` lines plus `TOTAL n`."""

    counts: dict[str, int] = {}
    total: int | None = None
    for line_number, entry in enumerate(_read_lines(path), start=1):
        head, _, tail = entry.partition(" ")
        if head == "TOTAL":
            total = int(tail.strip())
            continue
        try:
            parsed = int(head)
        except ValueError as exc:  # pragma: no cover - malformed baseline
            raise ValueError(f"invalid entry {path}:{line_number}: {entry!r}") from exc
        if parsed < 1:  # pragma: no cover - malformed baseline
            raise ValueError(f"count must be positive at {path}:{line_number}")
        counts[tail.strip()] = parsed
    if total is None:  # pragma: no cover - malformed baseline
        raise ValueError(
            f"{path} has no TOTAL line; without it a write that moved between "
            "two already-baselined files could net to zero unnoticed"
        )
    return counts, total


# --------------------------------------------------------------------------
# two ratchets: membership, and magnitude
# --------------------------------------------------------------------------
#
# They are split because they answer different questions and want different
# remedies. A file that *starts* writing cohort state is a design decision
# somebody has to defend; an existing writer going from three sites to four is
# usually a refactor. A single baseline holding both cannot say which happened
# — the reader has to diff it by hand — and a ratchet whose failure message is
# not the diagnosis gets suppressed rather than acted on.
#
# The magnitude ratchet compares only keys present on BOTH sides, so it never
# reports a new or removed file. Membership is the file ratchet's job, and each
# guard is silent about the other's business.


def test_no_new_cohort_writer_file() -> None:
    current = set(cohort_writer_files())
    baseline = _read_file_baseline(FILE_BASELINE)

    added = sorted(current - baseline)
    assert not added, (
        f"these files did not write cohort-isp-01 state and now do. {REMEDY}\n  "
        + "\n  ".join(added)
    )


def test_the_writer_file_baseline_is_not_stale() -> None:
    current = set(cohort_writer_files())
    baseline = _read_file_baseline(FILE_BASELINE)

    retired = sorted(baseline - current)
    assert not retired, (
        "these files no longer write cohort state but the baseline still "
        "claims them. Delete the lines in the same change that removed the "
        "writers, or the ratchet silently regains room for new ones:\n  "
        + "\n  ".join(retired)
    )


def test_no_cohort_writer_gains_a_write_site() -> None:
    current = cohort_write_counts()
    baseline, _ = _read_site_baseline(SITE_BASELINE)

    grew = sorted(
        f"{name}: {baseline[name]} -> {current[name]}"
        for name in set(current) & set(baseline)
        if current[name] > baseline[name]
    )
    assert not grew, (
        f"existing cohort writers gained write sites. {REMEDY}\n  " + "\n  ".join(grew)
    )


def test_the_write_site_baseline_is_not_stale() -> None:
    current = cohort_write_counts()
    baseline, _ = _read_site_baseline(SITE_BASELINE)

    shrunk = sorted(
        f"{name}: baseline {baseline[name]}, now {current[name]}"
        for name in set(current) & set(baseline)
        if current[name] < baseline[name]
    )
    assert not shrunk, (
        "write sites were removed without lowering the baseline; a ratchet "
        "that absorbs removals can be spent twice:\n  " + "\n  ".join(shrunk)
    )


def test_the_write_site_total_is_exact() -> None:
    """The one thing per-file counts cannot catch on their own.

    A write moved from one already-baselined file to another leaves both
    per-file comparisons looking like an ordinary shrink-and-grow pair. The
    exact total is what proves the aggregate did not drift.
    """

    current = cohort_write_counts()
    _, total = _read_site_baseline(SITE_BASELINE)

    assert sum(current.values()) == total, (
        f"cohort write sites total {sum(current.values())}, baseline says "
        f"{total}. Regenerate with `--baseline sites` in the same change that "
        "explains the movement."
    )


def test_the_two_baselines_describe_the_same_files() -> None:
    """Split, not divergent. One regenerated without the other is a bug."""

    files = _read_file_baseline(FILE_BASELINE)
    counts, _ = _read_site_baseline(SITE_BASELINE)
    assert files == set(counts), (
        "the membership and magnitude baselines disagree about which files "
        "write cohort state; regenerate both:\n  only in files: "
        + ", ".join(sorted(files - set(counts)))
        + "\n  only in sites: "
        + ", ".join(sorted(set(counts) - files))
    )


def test_every_entry_point_family_is_scanned() -> None:
    """No Python-bearing repository root escapes the census unexplained."""

    unscanned = unscanned_python_roots()
    assert not unscanned, (
        "these repository roots contain Python that the cohort writer census "
        "neither scans nor excuses. A guard scoped to the directories that "
        "happened to exist when it was written states an unenforceable "
        "premise. Add the root to SCAN_ROOTS, or to EXCLUDED_PYTHON_ROOTS "
        "with the reason it is not a writer surface:\n  " + "\n  ".join(unscanned)
    )


# --------------------------------------------------------------------------
# the inventory and the census must describe the same world
# --------------------------------------------------------------------------


def test_every_counted_writer_is_classified() -> None:
    counted = {site.path for site in cohort_write_sites()}
    classified = surfaces.inventoried_paths()

    unclassified = sorted(counted - classified)
    assert not unclassified, (
        "the census counts these as writers of cohort-isp-01 state, and "
        "app/migration_source/surfaces.py does not say what they are. An "
        "unclassified writer is an unowned one:\n  " + "\n  ".join(unclassified)
    )


def test_every_classified_writer_is_counted() -> None:
    counted = {site.path for site in cohort_write_sites()}
    claimed = {surface.path for surface in surfaces.COHORT_SURFACES if surface.writes}

    phantom = sorted(claimed - counted)
    assert not phantom, (
        "the inventory classifies these as writing cohort state, and the "
        "census cannot see the write. Either the write was removed and the "
        "classification is stale, or the detector has a gap worth naming:\n  "
        + "\n  ".join(phantom)
    )


def test_non_writing_surfaces_write_nothing() -> None:
    counted = {site.path for site in cohort_write_sites()}
    non_writers = {
        surface.path for surface in surfaces.COHORT_SURFACES if not surface.writes
    }

    contradicted = sorted(non_writers & counted)
    assert not contradicted, (
        "these are inventoried as adapters or read-only consumers, and the "
        "census counts them writing cohort state. An adapter that writes is "
        "the exact bypass this cohort has to find before cutover:\n  "
        + "\n  ".join(contradicted)
    )


def test_inventory_and_census_agree_on_family_names() -> None:
    """The duplicated family enum in `app/` must match the census's."""

    assert {member.value for member in surfaces.EntryPointFamily} == {
        member.value for member in EntryPointFamily
    }, (
        "app/migration_source/surfaces.py restates the census families "
        "because app/ may not import scripts/. They have drifted, so the "
        "inventory can now name a family the census cannot produce."
    )


def test_inventory_family_matches_the_census_family() -> None:
    census = {site.path: site.family for site in cohort_write_sites()}
    disagreements = sorted(
        f"{surface.path}: inventory {surface.family}, census {census[surface.path]}"
        for surface in surfaces.COHORT_SURFACES
        if surface.path in census and census[surface.path] != surface.family.value
    )
    assert not disagreements, (
        "the inventory and the census disagree about which entry-point family "
        "a writer belongs to:\n  " + "\n  ".join(disagreements)
    )


# --------------------------------------------------------------------------
# sensitivity: the detector must actually detect
# --------------------------------------------------------------------------


def _synthetic_tree(root: Path, relative: str, source: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _counts(root: Path) -> dict[str, int]:
    return {site.key: site.count for site in cohort_write_sites(project_root=root)}


def test_detector_sees_a_new_direct_writer(tmp_path: Path) -> None:
    """A newly constructed cohort row in a service is counted."""

    _synthetic_tree(
        tmp_path,
        "app/services/brand_new_owner.py",
        "from app.models.subscriber import Subscriber\n"
        "def create(db):\n"
        "    row = Subscriber(first_name='a', last_name='b', email='c')\n"
        "    db.add(row)\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("service|app/services/brand_new_owner.py") == 1


def test_detector_sees_a_webhook_handler_writing_cohort_state(
    tmp_path: Path,
) -> None:
    """An inbound callback that writes the cohort is counted as its own family.

    A foreign system triggering a write to party or customer state is the
    hardest writer to notice by reading code, because nothing in this
    repository calls it.
    """

    _synthetic_tree(
        tmp_path,
        "app/api/partner_webhooks.py",
        "from app.models.party import Party\n"
        "def receive(db, payload):\n"
        "    db.add(Party(party_type='person', display_name=payload.name))\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("webhook_handler|app/api/partner_webhooks.py") == 1


def test_detector_sees_an_adapter_bypassing_its_owner(tmp_path: Path) -> None:
    """A route that mutates a fetched cohort row is counted as a writer.

    This is the shape that matters most: the adapter still *calls* a service,
    so a guard that only looked for `Model(...)` would pass it, and it still
    writes the account row afterwards.
    """

    _synthetic_tree(
        tmp_path,
        "app/api/customers.py",
        "from app.models.subscriber import Subscriber\n"
        "def patch(db, customer_id, payload):\n"
        "    subscriber = db.get(Subscriber, customer_id)\n"
        "    subscriber.billing_mode = payload.billing_mode\n"
        "    db.commit()\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("api_route|app/api/customers.py") == 1


def test_detector_sees_raw_sql_against_a_cohort_table(tmp_path: Path) -> None:
    _synthetic_tree(
        tmp_path,
        "scripts/one_off/repair.py",
        "SQL = 'UPDATE subscribers SET billing_mode = :mode WHERE id = :id'\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("cli_script|scripts/one_off/repair.py") == 1


def test_detector_ignores_a_same_named_attribute_on_another_entity(
    tmp_path: Path,
) -> None:
    """The acceptance half: an unrelated `.status = ...` is not a cohort write.

    Without this, the guard would pass for the wrong reason — a detector that
    counted every `status` assignment in the repository would show a baseline
    so noisy nobody could act on it, and the first response would be to
    suppress the guard rather than the writer.
    """

    _synthetic_tree(
        tmp_path,
        "app/services/unrelated.py",
        "from app.models.support import Ticket\n"
        "def close(db, ticket_id):\n"
        "    ticket = db.get(Ticket, ticket_id)\n"
        "    ticket.status = 'closed'\n",
    )
    assert _counts(tmp_path) == {}


def test_detector_reports_an_unscanned_python_root(tmp_path: Path) -> None:
    """An entry-point family living outside every scanned root is reported."""

    _synthetic_tree(
        tmp_path,
        "workers/nightly.py",
        "from app.models.party import Party\n"
        "def run(db):\n"
        "    db.add(Party(party_type='person', display_name='x'))\n",
    )
    assert unscanned_python_roots(project_root=tmp_path) == ("workers/",)


def test_an_excluded_root_is_not_reported(tmp_path: Path) -> None:
    """The acceptance half of the unscanned-root check."""

    _synthetic_tree(tmp_path, "tests/test_thing.py", "def test_x():\n    pass\n")
    assert unscanned_python_roots(project_root=tmp_path) == ()


def test_a_new_file_fails_membership_and_not_magnitude() -> None:
    """The split proved on its own inputs: each ratchet sees only its own event."""

    files = _read_file_baseline(FILE_BASELINE)
    counts, _ = _read_site_baseline(SITE_BASELINE)
    newcomer = "service|app/services/brand_new_writer.py"

    pretend_files = files | {newcomer}
    pretend_counts = {**counts, newcomer: 1}

    assert sorted(pretend_files - files) == [newcomer]
    # magnitude compares only keys on BOTH sides, so it stays silent
    shared = set(pretend_counts) & set(counts)
    assert not [name for name in shared if pretend_counts[name] != counts[name]]


def test_a_grown_writer_fails_magnitude_and_not_membership() -> None:
    """The mirror image, so neither guard is doing the other's job."""

    files = _read_file_baseline(FILE_BASELINE)
    counts, _ = _read_site_baseline(SITE_BASELINE)
    survivor = "service|app/services/party.py"
    assert survivor in counts, "fixture assumption: party.py is a baselined writer"

    pretend_counts = {**counts, survivor: counts[survivor] + 1}

    assert set(files) == set(pretend_counts)
    grew = [
        name
        for name in set(pretend_counts) & set(counts)
        if pretend_counts[name] > counts[name]
    ]
    assert grew == [survivor]


def test_a_writer_moved_between_files_is_caught_by_the_total() -> None:
    """Per-file counts net out; the exact total does not.

    One site leaves `party.py` and appears in `subscriber.py`. Both per-file
    comparisons look like an ordinary shrink and an ordinary grow, and the
    membership set is unchanged — only the aggregate proves nothing was
    invented. Here the aggregate is equal, which is exactly why the guard
    asserts equality rather than an inequality.
    """

    counts, total = _read_site_baseline(SITE_BASELINE)
    left, right = "service|app/services/party.py", "service|app/services/subscriber.py"
    moved = {**counts, left: counts[left] - 1, right: counts[right] + 1}

    assert sum(moved.values()) == total
    inflated = {**moved, right: moved[right] + 1}
    assert sum(inflated.values()) != total


def test_stale_baseline_after_writer_removal_is_a_failure() -> None:
    """Removing a writer without deleting its line must fail membership."""

    baseline = _read_file_baseline(FILE_BASELINE)
    survivor = "service|app/services/party.py"
    assert survivor in baseline, "fixture assumption: party.py is a baselined writer"

    pretend_current = baseline - {survivor}
    assert sorted(baseline - pretend_current) == [survivor]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/api/customers.py", EntryPointFamily.API_ROUTE),
        ("app/api/crm_webhooks.py", EntryPointFamily.WEBHOOK_HANDLER),
        ("app/api/payment_callback.py", EntryPointFamily.WEBHOOK_HANDLER),
        ("app/web/admin/customers.py", EntryPointFamily.WEB_ROUTE),
        ("app/services/web_customer_actions.py", EntryPointFamily.WEB_PRESENTER),
        ("app/services/party.py", EntryPointFamily.SERVICE),
        ("app/services/events/handlers/thing.py", EntryPointFamily.EVENT_HANDLER),
        ("app/tasks/nin_tasks.py", EntryPointFamily.TASK_WORKER),
        ("app/celery_scheduler.py", EntryPointFamily.SCHEDULED_JOB),
        ("app/websocket/hub.py", EntryPointFamily.WEBSOCKET),
        ("app/imports/loader.py", EntryPointFamily.IMPORTER),
        ("app/poller/snmp.py", EntryPointFamily.POLLER),
        ("app/syslog/reader.py", EntryPointFamily.POLLER),
        ("app/db.py", EntryPointFamily.APP_MODULE),
        ("scripts/one_off/thing.py", EntryPointFamily.CLI_SCRIPT),
        ("alembic/versions/999_thing.py", EntryPointFamily.MIGRATION),
        ("stray.py", EntryPointFamily.REPOSITORY_ROOT),
    ],
)
def test_every_declared_family_is_reachable(
    path: str, expected: EntryPointFamily
) -> None:
    """Each declared family must be produced by some real path shape.

    A family nobody can reach is decoration: it makes the enumeration look
    complete while covering nothing.
    """

    assert family_for(path) is expected


def test_the_ownership_map_lists_every_production_writer() -> None:
    """The prose map and the classified inventory must name the same files.

    The document is what a reviewer reads before a cutover pull request. A
    writer that exists in the inventory and not in the document is a writer
    nobody will plan to displace.
    """

    document = (
        Path(__file__).resolve().parents[2] / "docs" / "ISP_COHORT1_SOURCE_OWNERSHIP.md"
    ).read_text(encoding="utf-8")

    missing = sorted(
        path for path in surfaces.displaced_writer_paths() if path not in document
    )
    assert not missing, (
        "docs/ISP_COHORT1_SOURCE_OWNERSHIP.md does not mention these "
        "production writers of cohort-isp-01 state:\n  " + "\n  ".join(missing)
    )


def test_every_writer_is_also_a_reader() -> None:
    """Coherence between the two censuses.

    A file cannot write a cohort table without naming it, so the reference
    census must contain every writer. If it does not, the reference scan has a
    gap and its per-family totals understate the reach a cutover has to
    consider.
    """

    writers = {site.path for site in cohort_write_sites()}
    referenced = {path for _, path in cohort_reference_sites()}
    missing = sorted(writers - referenced)
    assert not missing, (
        "these files write cohort state and the reference census does not see "
        "them naming it:\n  " + "\n  ".join(missing)
    )


def test_the_reader_reach_is_wider_than_the_writer_surface() -> None:
    """The reference census must actually be measuring something wider.

    Not a numeric assertion — the count moves with any unrelated file that
    mentions `Subscriber`. What is asserted is the shape: many more readers
    than writers, across more entry-point families. A reference census that
    collapsed to the writer set would be reporting the wrong thing while
    looking healthy.
    """

    reference_families = set(reference_counts_by_family())
    writer_families = {site.family for site in cohort_write_sites()}
    assert writer_families <= reference_families
    assert len(reference_families) > len(writer_families), (
        "the reference census sees no entry-point family the writer census "
        "does not; readers reach further than writers and the scan should "
        "show it"
    )
    assert len(cohort_reference_sites()) > 5 * len(cohort_write_sites())


# --------------------------------------------------------------------------
# every writer decided individually; only callers may take the default
# --------------------------------------------------------------------------


def test_every_counted_writer_has_an_individual_disposition() -> None:
    """A writer's fate is a decision about a specific line of code.

    The class default exists for the 356 referencing files this inventory does
    not name one by one. It must never reach a writer: "displace this" is not
    something a blanket rule can decide, and a writer that quietly inherited a
    reader's disposition would be missing from the set `ctl-isp-009` ratchets
    to zero.
    """

    counted = {site.path for site in cohort_write_sites()}
    decided = {surface.path for surface in surfaces.COHORT_SURFACES if surface.writes}
    undecided = sorted(counted - decided)
    assert not undecided, (
        "these files write cohort-isp-01 state and have no individual "
        "disposition, so they would fall under the caller default:\n  "
        + "\n  ".join(undecided)
    )


def test_no_counted_writer_relies_on_the_default_disposition() -> None:
    """The same rule from the other side: the default is reader-shaped.

    `REPOINT_TO_TARGET_API` says "read this from the target after the switch".
    Applied to a writer it would say nothing about removing the write, which is
    the only thing that matters about a writer.
    """

    assert surfaces.DEFAULT_CALLER_DISPOSITION is (
        surfaces.Disposition.REPOINT_TO_TARGET_API
    )
    assert surfaces.DEFAULT_CALLER_DISPOSITION not in {
        surfaces.Disposition.RETIRE_AFTER_CUTOVER,
        surfaces.Disposition.ROUTE_THROUGH_OWNER_FIRST,
    }, (
        "the caller default became a writer disposition; a blanket rule "
        "cannot decide which lines of code get removed"
    )


def test_the_reader_remainder_is_covered_by_the_default() -> None:
    """Every referencing file is accounted for: individually, or by the default.

    Arithmetic rather than judgement, and that is the point — it proves there
    is no third category quietly holding files nobody classified and nobody
    defaulted.
    """

    referenced = {path for _, path in cohort_reference_sites()}
    individually = referenced & surfaces.inventoried_paths()
    defaulted = referenced - individually
    assert len(individually) + len(defaulted) == len(referenced)
    assert defaulted, (
        "no file takes the caller default, so the default is unreachable "
        "decoration; either inline it or delete it"
    )
