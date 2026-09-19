# Rationale — `idempotency_legacy_callers.txt`

`idempotency_legacy_callers.txt` is a two-directional ratchet baseline
(`tests/architecture/test_idempotency_legacy_ratchet.py`) and is read as a
bare, sorted, deduplicated list of file paths compared exactly against the
observed reference set — it cannot itself carry inline comments without
breaking that equality check. This file records, alongside the baseline,
the reviewed reason each addition was accepted rather than refused, so
"new callers are forbidden" stays a reviewed containment boundary and not a
silent expansion.

## `app/services/catalog/offer_access_requirement.py` — added 2026-09-16

**Why a 33rd caller is warranted.** This module is the sole owner of
access-classified offer-version admission (`AdmitOfferVersionCommand`) and
reviewed legacy-version classification. Both owner commands need
replay-safe, exactly-once admission under a caller-supplied idempotency key
(concurrent retries, at-least-once delivery from upstream callers). The
module reserves and consumes rows in the shared `idempotency_keys` ledger
via `app.models.idempotency.IdempotencyKey`, in the same hand-rolled
reservation-over-the-model shape as every other entry already in this
baseline (advisory lock, look up existing key in scope, compare stored
inputs, insert on first use, return the stored result on replay). It does
not introduce a new mechanism.

**Why there is no sanctioned alternative.**
`docs/adr/0011-module-lineage-composition.md`'s 2026-08-25 amendment
("a product revision may supply a runtime prerequisite") is explicit:
`IdempotencyKey`, `TaskExecution` and `idempotent_task` **remain
authoritative**, and `dotmac_kernel.idempotency` **remains forbidden under
`app/`**. There is no sanctioned facade over the model for a new owner
command to call instead — the only way to get replay-safety for
`offer_access_requirement`'s admission command today is the same
hand-rolled pattern the other 32 callers already use. Extracting a shared
facade now would itself have to import `IdempotencyKey`/`TaskExecution` and
would simply move the same reference into a new file, tripping this exact
ratchet without removing the underlying coexistence.

**Retirement.** This entry is coexistence debt, not a permanent grant. It
must be retired — removed from `idempotency_legacy_callers.txt` in the same
reviewed slice that migrates `offer_access_requirement`'s admission
idempotency off the local `IdempotencyKey` model — at the eventual kernel
idempotency cutover described by ADR-0011's amendment (pinned
exact-tagged kernel/module artifacts, composed plane, fresh/predecessor
upgrade and RLS proof, then a shadow/parity phase before any writer moves).
