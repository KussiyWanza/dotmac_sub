# Open PR consolidation: 19 September 2026

## Review scope

This branch consolidates the seven source heads below onto main commit
`3b79bb93715778a048b810f5f68f7c44b2810c1c`. Original commits are preserved
through merge commits. This document records development evidence, not release
or deployment authorization.

| Source PR | Preserved head | Change |
| --- | --- | --- |
| #3187 | `8fc3e32fa495fd23671da3c9b536339876b9be85` | Canonical invoice-accounting projection digest and contract-surface update |
| #3189 | `e62681a56ecedb698a9f7100fb40b9be8b572fff` | Distinct billed and collected revenue-trend colors |
| #3191 | `6c4fe0c04703f6156dca3068ecccaf857344cb35` | Typed invoice-list projection, customer selection, created-date visibility, and combined pagination coverage |
| #3192 | `1548756bab2519c1443c2e265952309743fccc5d` | Huawei HG8546M Wi-Fi key path plus the branch's outage, billing, and notification changes |
| #3193 | `d2c997d033a3679147a3255b1b7fc4063a53bb99` | Prepaid settlement anchoring and reviewed double-extension repair |
| #3194 | `726bc4d28fc04d56a621faa329deaebe0fe1c1a4` | Fiber acquisition attribution, conversion projection, migration, and runbook |
| #3195 | `f5afc84ce7d9eb23943c86139778b3ca85d7e000` | Intersecting invoice filters, account-safe reset, and collectible unpaid semantics |

Draft PR #3155 at head `e73b1312a587217c85758d476084a8ad563362ce`
is explicitly excluded and untouched. Its 13 commits unique to current main
were checked against the consolidation history and none are present. The
consolidation does not change `VERSION` or any CI workflow.

## Integration repairs

1. The outage infrastructure-ticket audit call keeps main's current typed
   `AuditActor` boundary while preserving #3192's ticket-creation command and
   owner contract.
2. The overlapping invoice-filter PRs are combined rather than choosing one.
   The final projection retains #3191's typed customer selection and UTC
   created-date display, plus #3195's intersecting account/customer filters,
   account-safe clear URL, proforma refresh, and collectible `unpaid` rule.
   Focused regression coverage from both branches is retained.
3. The Huawei HG8546M parameter path is formatted to the repository's current
   Ruff contract without changing its value.

## Development evidence

- Every included source head is an ancestor of the consolidation head.
- No unique commit from excluded PR #3155 is an ancestor of the consolidation.
- `python -m ruff check app tests scripts alembic` passed.
- `python -m ruff format --check app tests scripts alembic` passed after the
  integration formatting repair.
- `python -m scripts.architecture.sot_manifest_docs` reported the generated
  relationship map current.
- Python compilation passed for the conflict-resolved service and regression
  files.
- Local mypy, import-linter, Bandit, and focused pytest execution could not
  start because this checkout lacks their development dependencies (including
  `mypy`, `lint_imports`, `bandit`, and `fastapi`). GitHub CI remains the
  validation authority for those lanes.

These checks do not replace the repository's full CI. Acceptance requires all
required checks on the final PR revision, including unit and PostgreSQL shards,
architecture, type/import/security checks, browser coverage, engineering
standards, and release/version gates. Source ancestry and exclusion must be
rechecked on that exact revision.
