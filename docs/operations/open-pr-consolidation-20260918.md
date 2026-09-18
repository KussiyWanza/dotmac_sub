# Open PR consolidation: 18 September 2026

## Review scope

PR #3186 consolidates the seven source heads below onto main commit
`0ba2f8a7228219928b3e24296758dc8ab995313c`. Original commits are preserved
through merge commits. This document records development evidence, not release
or deployment authorization.

| Source PR | Preserved head | Change |
| --- | --- | --- |
| #3178 | `122d69a4ba20feec13d7e76de40d81a060670ea5` | Prepaid renewal review-item savepoint recovery |
| #3179 | `cbeb42e233cf0b5c52b20b42da429d245f8c7d0e` | Material-status capability-read transaction boundary |
| #3180 | `082f09e290b37c98153ac08f5cd1e29b3d66d490` | AI intake instructions aligned with the strict facts schema |
| #3181 | `e1679eab66924596d8de1fce42efa2003a1c8f73` | Retirement of obsolete expense outbox events |
| #3182 | `c4fe607bc0ecd541e712272c421014d1363a7e62` | Isolated FUP contention and bounded targeted retries |
| #3183 | `7cbf7d13679e4d7792f4d88e6d87a01f2cd2d17b` | Accurate OLT backup failure and incomplete-run outcomes |
| #3185 | `078bee2f7e21af5da4e113bb4c4864fb5499192b` | Invoice references in ledger CSV and historical billing dates |

PR #3155 and PR #3184 are explicitly excluded. Their unique commits were
checked against the consolidation's added history and were absent. The
consolidation does not change `VERSION` or any CI workflow.

## Integration repairs

1. Invalid AI facts still fail strict validation. Regression tests now exercise
   both an available clarification turn and an exhausted clarification budget,
   instead of confusing classifier failure count with follow-up count. The
   runtime retry policy is unchanged.
2. Ledger CSV tests validate named columns, including invoice references,
   debit and credit amounts, empty references and quoted reference values.
3. Payment preview now forwards the selected payment date to the same payload
   builder used by confirmation. Regression coverage verifies historical UTC
   receipt dates, pending-payment timestamps, future-date rejection and date
   propagation through preview. Linked billing guidance describes the invoice
   date permission and payment/export behavior.
4. The existing refresh owner now samples its default decision time after
   acquiring the session row lock. Deterministic tests reproduce both false
   duplicate revocation and an expired overlap accepted with a stale clock.
   The fix retains the five-second replay limit, same-client requirement,
   fail-closed revocation and explicit observed-at semantics. The owning
   registry contract and authentication design explain the clock boundary.

## Development evidence

- Assembly run `35380726876` verified source ancestry, excluded unique commits,
  unchanged release/CI files, Python compilation and clean diff whitespace.
- Billing/AI repair commit `9ee4a01b4c67ae8500bedc4456633fdcf56f38fb` passed
  279 focused tests, repository-wide Ruff lint/format checks and workflow
  guidance validation in run `35381813592`.
- Authentication repair commit `bb82b8665bcda6e801a0445a4d4e330726685043`
  passed 294 focused behavior and architecture tests plus repository-wide
  Ruff lint/format and generated-manifest validation in run `35382134035`.
  Both new clock regressions failed before the fix. The existing concurrent
  refresh test then passed five separate runs against PostgreSQL/PostGIS after
  the repository's real migration/bootstrap sequence.

These focused runs do not replace the repository's full CI. Acceptance requires
all required checks on the final PR revision, including the complete unit and
PostgreSQL shards, architecture, type/import/security checks, browser gate,
engineering standards and release/version gates. Source heads must still be
ancestors of that exact revision, and the excluded unique commits must remain
absent. Review the current PR checks rather than treating an earlier run as
acceptance of a later commit.
