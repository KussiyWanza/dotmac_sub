r"""Every strict-JSON evidence document under docs/ must parse as JSON.

Michael's ruling on this repair: "strict JSON evidence files must be outside
Ruff's formatting ownership, while their validator runs after formatting and
fails on invalid JSON. Don't 'fix' this by accepting Ruff-formatted
pseudo-JSON."

``ruff format`` rewrites ``docs/kernel-runtime-composition.json`` into
pseudo-JSON with a trailing comma before the closing ``}``/``]`` — confirmed
directly against this repository (copy the file, run ``ruff format`` on the
copy, and ``json.load`` the result: ``json.decoder.JSONDecodeError: Illegal
trailing comma before end of object``). ``docs/kernel-runtime-readiness.json``,
untouched by the change that added this test, shows the identical
"Would reformat" from ``ruff format --check`` — this is pre-existing and
formatter-shaped, not specific to one document's content.

The repair has two parts. ``pyproject.toml``'s ``[tool.ruff] extend-exclude``
and ``.pre-commit-config.yaml``'s per-hook ``exclude:`` (a directory-walk
exclude does not help against a pre-commit hook that passes a changed file's
exact path) keep the formatter off both documents entirely. THIS test is the
other half: it runs ``json.load`` directly against both documents and fails,
naming the path, on any parse error — so if the formatter's exclusion is ever
removed or bypassed, this test (not a human reading a diff) is what catches
the corruption. Order matters: this must run as part of the normal test
suite, never a "does ruff accept it" check that a corrupting formatter step
could run before and mask.

This module and the ``.github/workflows/ci.yml`` "Strict-JSON evidence
documents parse after formatting" step are complementary for a THIRD reason,
beyond the two already recorded in that step's comment (formatter-in-this-
run vs. arrived-broken-by-any-route): the workflow step is skippable BY
CONFIGURATION in a way this test is not. The `changes` job's docs-only
classifier (`grep -Ev '^(docs/|.*\.md$)'`) treats a PR that changes only
``docs/kernel-runtime-composition.json`` as docs-only, and every OTHER step
in the same `lint` job is guarded off on a docs-only change — so a workflow
maintainer "tidying up" an apparently-inconsistent guard could reattach
`if: needs.changes.outputs.docs-only != 'true'` to that one step and silence
it on precisely the PRs most likely to corrupt the record it protects, while
every other CI signal stays green. This module has no such knob: it runs
whenever the `architecture` test job runs, full stop, with no path-based
classifier able to skip it.
`test_the_ci_strict_json_step_is_not_gated_on_docs_only` below additionally
pins that the workflow step itself carries no `docs-only` guard, so a
reintroduced guard is caught here too, not only by a human reading the
workflow file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"

#: The exact set of strict-JSON evidence documents this validator owns.
#: Adding a new one is a deliberate change to this tuple, not something a
#: glob silently discovers or silently misses.
STRICT_JSON_EVIDENCE_DOCUMENTS = (
    DOCS_ROOT / "kernel-runtime-composition.json",
    DOCS_ROOT / "kernel-runtime-readiness.json",
)


@pytest.mark.parametrize("path", STRICT_JSON_EVIDENCE_DOCUMENTS, ids=lambda p: p.name)
def test_strict_json_evidence_document_parses(path: Path) -> None:
    assert path.is_file(), f"{path} does not exist"
    try:
        with path.open(encoding="utf-8") as handle:
            json.load(handle)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{path} is not valid JSON: {exc}. If this started failing after "
            "a formatter ran over it, the fix is to exclude the document "
            "from the formatter's ownership (pyproject.toml [tool.ruff] "
            "extend-exclude and .pre-commit-config.yaml's per-hook "
            "exclude), never to accept the formatter's output."
        ) from exc


def test_a_trailing_comma_document_is_refused_by_this_validator() -> None:
    """Sensitivity plant: without this case, the parametrized test above
    passes on a clean tree for the wrong reason — it would pass identically
    whether or not it is actually capable of catching a malformed document.
    A synthetic document shaped exactly like ruff-format's known output
    (a trailing comma before the closing brace) must fail json.load."""
    trailing_comma_pseudo_json = (
        '{\n  "schema_version": "dimensional-composition.v3",\n}\n'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(trailing_comma_pseudo_json)


def test_the_ci_strict_json_step_is_not_gated_on_docs_only() -> None:
    """Sensitivity proof for the third complementarity reason recorded in the
    module docstring: the `lint` job's "Strict-JSON evidence documents parse
    after formatting" step, and the `actions/checkout@v4` step immediately
    above it in the same job, must carry no `docs-only` condition — a PR
    that changes only docs/kernel-runtime-composition.json sets
    `docs-only=true`, and every OTHER step in that job IS correctly guarded
    off in that case, so a guard reattached here by someone "tidying up an
    inconsistency" would silence the one check on exactly the PRs most
    likely to need it. Planted-defect shape: this test fails loudly, naming
    the step, if either `if:` key ever reappears."""
    import yaml

    workflow = yaml.safe_load(
        (DOCS_ROOT.parent / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
    )
    lint_job = workflow["jobs"]["lint"]
    steps_by_name = {
        step.get("name") or step.get("uses"): step for step in lint_job["steps"]
    }

    json_step = steps_by_name["Strict-JSON evidence documents parse after formatting"]
    assert "if" not in json_step, (
        "the strict-JSON step must run unconditionally in the lint job — "
        "a docs-only guard here would skip it on exactly the PRs that "
        "change only the record it validates"
    )

    checkout_step = steps_by_name["actions/checkout@v4"]
    assert "if" not in checkout_step, (
        "actions/checkout@v4 in the lint job must run unconditionally — "
        "the strict-JSON step depends on the checked-out working tree, and "
        "a docs-only guard on checkout would leave it with no files to open"
    )

    # The job itself must still admit a docs-only run, or none of the above
    # matters: an unguarded step inside a job that never executes on a
    # docs-only change would be reachability theatre. The job's own `if:`
    # ORs in `docs-only == 'true'` as an alternative to requiring
    # python-environment to have succeeded — that disjunct is what keeps the
    # job reachable on a docs-only PR.
    assert "needs.changes.outputs.docs-only == 'true'" in lint_job.get("if", ""), (
        "the lint job's own condition no longer admits a docs-only run — "
        "the strict-JSON step would then be unreachable on exactly the "
        "PRs it exists to cover"
    )


def test_strict_json_evidence_documents_are_excluded_from_ruff_formatting() -> None:
    """Static proof that the formatter-ownership half of the repair is in
    place, not just the validator half: pyproject.toml's [tool.ruff]
    extend-exclude must name both documents by their exact repository-
    relative path (not a docs/ wildcard — this repair is scoped to strict-
    JSON evidence documents, not documentation generally)."""
    import tomllib

    pyproject = tomllib.loads(
        (DOCS_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    excluded = set(pyproject["tool"]["ruff"].get("extend-exclude", ()))
    for path in STRICT_JSON_EVIDENCE_DOCUMENTS:
        relative = path.relative_to(DOCS_ROOT.parent).as_posix()
        assert relative in excluded, (
            f"{relative} must be named exactly in [tool.ruff] extend-exclude"
        )
    # Scoped to the two documents, not docs/ wholesale.
    assert "docs" not in excluded
    assert "docs/" not in excluded
