"""The CRM/Omni surface is frozen while it is being replaced domain by domain.

The `dotmac_crm` deployment ("Omni") was decommissioned 2026-08-29. What
remains inside Sub is replaced one domain at a time — Inbox/Chat, then
Support/Ticketing, Sales/Quotes, Party/Customer/Reseller, ERP modules, and
finally secrets/deployment/observability — each slice deleting residue BESIDE
its replacement owner rather than in a sweep.

That only works if the surface holds still between slices, and this is what
holds it. It is deliberately two-directional:

* A **new** CRM or Omni dependency must not land while the programme runs.
* An **existing** one must not vanish silently. Every removal lowers the
  baseline in the same change, which is what turns the programme into a
  sequence of recorded decisions instead of a diff nobody can audit.

The falling direction is the one people find surprising, so it is worth being
explicit: yes, deleting CRM code fails this guard. That is the guard working.
Lower the baseline in the same commit and it passes.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.crm_vocabulary import (
    CRM_TERMS,
    LANES,
    _is_characterization_only,
    mentions_crm,
    surface_by_lane,
    surface_paths,
    tokens,
)

BASELINE = Path("tests/architecture/crm_vocabulary_baseline.txt")


def _baseline() -> frozenset[str]:
    return frozenset(
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


# ── the two directions ───────────────────────────────────────────────────────


def test_no_new_crm_dependency_lands() -> None:
    added = sorted(surface_paths() - _baseline())
    assert not added, (
        "These files newly reference the CRM/Omni vocabulary. The CRM is gone "
        "and its surface is being retired domain by domain — a new dependency "
        "on it cannot land while that is in progress. If this is a rename or "
        "a move, lower the baseline for the old path in the same change: "
        f"{added}"
    )


def test_no_crm_dependency_disappears_unrecorded() -> None:
    removed = sorted(_baseline() - surface_paths())
    assert not removed, (
        "These baselined files no longer carry the CRM/Omni vocabulary. If "
        "that is your slice's intended removal, delete these lines from "
        "tests/architecture/crm_vocabulary_baseline.txt in the SAME change — "
        "the freeze records every removal deliberately. If you did not mean "
        f"to remove them, something else did: {removed}"
    )


def test_recorded_lane_totals_match_the_listed_paths() -> None:
    """The human-readable per-lane counts must not drift from the list."""

    text = BASELINE.read_text(encoding="utf-8")
    current = surface_by_lane()
    for lane in LANES:
        marker = f"# --- {lane}: {len(current[lane])} files ---"
        assert marker in text, (
            f"the baseline's {lane} header does not match reality "
            f"({len(current[lane])} files) — update the header with the list"
        )


# ── sensitivity proof ────────────────────────────────────────────────────────


def test_the_detector_sees_the_identifier_forms_a_word_match_would_miss() -> None:
    """The whole reason this uses tokens rather than `\\b`.

    A word boundary does not fire beside `_` or a digit, so `\\bcrm\\b` reads
    every one of these as clean — and they are nearly the entire real surface.
    Without this proof the two ratchet assertions above would pass over a
    detector that had quietly stopped matching anything.
    """

    for identifier in (
        "crm_subscriber_id",
        "crm_ticket_pull",
        "CRMClient",
        "resolve_crm_subscriber_id",
        "dotmac.crm",
        "dotmac_omni",
        "omni_id",
        "CRM_TICKET_PULL_ENABLED",
        "crm.ticket_observation.v1",
        "work_order_mirror = CRMWorkOrder",
    ):
        assert mentions_crm(identifier), (
            f"the detector missed {identifier!r} — this is the identifier "
            "shape the freeze exists to see"
        )


def test_a_word_boundary_match_really_would_have_missed_them() -> None:
    """Pin the premise, so the docstring above cannot rot into folklore."""

    import re

    naive = re.compile(r"\bcrm\b")
    for identifier in ("crm_subscriber_id", "CRMClient", "crm_ticket_pull"):
        assert not naive.search(identifier), (
            f"`\\bcrm\\b` now matches {identifier!r}; the reason this module "
            "tokenises instead of word-matching needs restating"
        )
        assert mentions_crm(identifier)


def test_the_detector_does_not_invent_members() -> None:
    """Specificity. A guard that fires on everything gets deleted."""

    for innocent in (
        "scrum board",
        "omnichannel routing",
        "microphone",
        "incremental",
        "e1de51fcf0e93869ce8776c6291f8b1ac4b0a35b373adcaa322c46e5c3f48908",
        "YWNybWFu",
    ):
        assert not mentions_crm(innocent), (
            f"the detector claimed {innocent!r} references the CRM; token "
            "equality is supposed to make substring accidents impossible"
        )


def test_camel_case_is_split_before_matching() -> None:
    assert "crm" in tokens("CRMClient")
    assert "crm" in tokens("fetchCRMTicket")
    assert tokens("crm_subscriber_id") == frozenset({"crm", "subscriber", "id"})


# ── scope ────────────────────────────────────────────────────────────────────


def test_the_freeze_covers_every_entry_point_family() -> None:
    """Families, not one directory: app (with its tasks and workers),
    migrations, operator scripts, the suite, and the programme's docs."""

    assert set(LANES) == {"app", "alembic", "docs", "scripts", "tests"}
    for lane in LANES:
        assert Path(lane).is_dir(), f"frozen lane {lane} no longer exists"
    assert Path("app/tasks").is_dir(), "the task/worker family moved out of app/"
    assert CRM_TERMS == frozenset({"crm", "omni"})


# ── dependency vs. mention: how the literal is consumed ─────────────────────


def _characterization_only(source: str) -> bool:
    import textwrap

    return _is_characterization_only(
        Path("scratch_test_module.py"), textwrap.dedent(source)
    )


def test_a_real_import_of_a_crm_module_still_counts_as_a_dependency() -> None:
    """Sensitivity proof (plant): a genuine `import`/`from ... import` of a
    CRM-named module is a real dependency and must never be exempted —
    regardless of whether the imported module happens to already be a
    frozen surface member. This is the case the exemption must NOT touch."""

    planted = """
        from app.services.crm_client import CRMClient

        def make_client():
            return CRMClient()
    """
    assert not _characterization_only(planted)


def test_a_dynamic_import_of_a_crm_module_still_counts_as_a_dependency() -> None:
    """Sensitivity proof (plant): a runtime import operation is exactly as
    real a dependency as a static import statement."""

    planted = """
        import importlib

        def load():
            return importlib.import_module("app.services.crm_client")
    """
    assert not _characterization_only(planted)


def test_a_production_read_of_a_crm_path_still_counts_as_a_dependency() -> None:
    """Sensitivity proof (plant, same string as the near-miss below): a
    production operation that actually TOUCHES a CRM/Omni path's content —
    opens it, reads it, or spawns it — is a live dependency on the retired
    surface even though the path appears only as a string argument, never
    an import. Locating a path is not the same operation as reading it, and
    only the first is characterization; this must not be exempted no matter
    how it is spelled."""

    bare_open = """
        def dump():
            with open("scripts/migration/backfill_crm_subscriber_links.py") as f:
                return f.read()
    """
    assert not _characterization_only(bare_open)

    chained_read = """
        from pathlib import Path

        def dump():
            target = Path("scripts/migration/backfill_crm_subscriber_links.py")
            return target.read_text()
    """
    assert not _characterization_only(chained_read)

    via_subprocess = """
        import subprocess

        def run_it():
            return subprocess.run(
                ["python", "scripts/migration/backfill_crm_subscriber_links.py"]
            )
    """
    assert not _characterization_only(via_subprocess)


def test_the_identical_name_used_only_to_locate_a_path_does_not_count() -> None:
    """Sensitivity proof (near-miss, same string as the plant above): the
    IDENTICAL path, constructed only to be located or described — never
    read, opened, or executed — is characterization data. Same string,
    opposite outcome, decided entirely by how it is consumed."""

    near_miss = """
        from pathlib import Path

        KNOWN_RESIDUE_EXAMPLE = Path("scripts/migration/backfill_crm_subscriber_links.py")

        def describe():
            return str(KNOWN_RESIDUE_EXAMPLE)
    """
    assert _characterization_only(near_miss)


def test_reading_the_freezes_own_ledger_is_still_characterization() -> None:
    """The one narrow, explicit exception: the ledger file this whole
    module is built around is characterization data by definition — a list
    of paths, never application code — so reading IT specifically does not
    count, unlike reading any other CRM/Omni path."""

    reads_the_ledger = """
        from pathlib import Path

        LEDGER = Path("tests/architecture/crm_vocabulary_baseline.txt")

        def load():
            return LEDGER.read_text(encoding="utf-8")
    """
    assert _characterization_only(reads_the_ledger)


def test_a_crm_name_inside_a_dict_or_list_still_counts_as_a_dependency() -> None:
    """Sensitivity proof (near-miss): a container literal is not, on its
    own, proof of inert data — the real shape at
    `app/services/infrastructure_health.py`, which keys a live health-check
    dispatch off a `"crm"` string in a plain dict. A blanket "any
    list/tuple/set/dict is a mention" rule would have silently exempted
    this genuine dependency, so it must NOT be exempted."""

    planted_dict = """
        HEALTH_CHECK_OWNERS = {"crm": "celery-worker"}

        def owner_for(service):
            return HEALTH_CHECK_OWNERS[service]
    """
    assert not _characterization_only(planted_dict)

    planted_list = """
        MONITORED_SERVICES = ["crm", "billing"]
    """
    assert not _characterization_only(planted_list)


def test_a_prose_or_identifier_mention_still_counts_as_a_dependency() -> None:
    """Sensitivity proof (near-miss): the exemption is narrow to ONE
    recognized data shape (`Path("literal")`). A bare identifier, an
    f-string, a docstring sentence, or a plain assignment are all
    unclassified and keep counting — the conservative default this
    exemption only ever subtracts from, never adds a blanket allowance to."""

    bare_identifier = 'CRM_DATABASE_URL = "postgresql://example/crm"'
    assert not _characterization_only(bare_identifier)

    docstring_prose = '''
        """This module talks to the CRM."""
    '''
    assert not _characterization_only(docstring_prose)


def test_the_freeze_is_not_measuring_an_empty_set() -> None:
    """705 files at a12b9ebca. A freeze over nothing passes for the wrong
    reason, and this is the assertion that notices if the scan breaks."""

    current = surface_by_lane()
    assert sum(len(paths) for paths in current.values()) > 500
    for lane in LANES:
        assert current[lane], f"lane {lane} reported no CRM surface at all"
