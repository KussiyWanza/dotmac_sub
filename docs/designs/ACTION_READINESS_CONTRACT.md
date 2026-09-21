# Action Readiness Contract

**Owner:** `app/services/action_readiness.py` (contract) +
`app/services/web_action_readiness.py` (render context) +
`app/schemas/action_readiness.py` (transport)
**Status:** Step 1 — contract + first real consumer (payment-proof review).
ONT/ACS is step 2 (a separate, already-dispatched change) and is the primary
operational proof (the Jabi reference case).
**Implements the shapes named in:** `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`

## Why

Every gated action in this codebase already has a domain owner that decides
whether it may run right now, and — when it can't — why not. That decision
keeps arriving in a different, ad-hoc shape per domain: a preflight dict here,
a typed `*ReviewEligibility` dataclass there, a literal `disabled_reason`
string composed inline at a web call site. Each shape works on its own, but
none of them compose: a caller that wants to show "why is this blocked" and
"what would clear it" next to a correlation id has to invent that shape every
time.

`ActionReadiness` gives every domain owner ONE transport-neutral shape to hand
that decision to any caller (API, web, mobile, worker), without moving who
decides it.

## What this is not

- **Not a workflow engine.** Nothing in this contract decides anything,
  persists anything, executes a repair, or dispatches a transition. It is
  pure vocabulary: frozen dataclasses that validate their own internal
  consistency.
- **Not a second authority.** A domain owner that adopts `ActionReadiness`
  keeps its existing eligibility/decision logic exactly as it was; only the
  shape handed to the caller changes. The payment-proof pilot in this change
  proves this with equivalence tests: the old `PaymentProofReviewEligibility`
  verdict and the new `ActionReadiness`-derived verdict agree on every
  existing scenario.
- **Not a generic cross-domain endpoint.** No route is mounted for the
  transport schema in this change. A generic "give me readiness for any
  action" endpoint would recreate the excluded global workflow engine through
  an implicit dispatch table; each consumer instead embeds
  `ActionReadinessRead` into its own existing response/context.

## The contract

| Type | Answers |
| --- | --- |
| `ReadinessState` | `ready`, `blocked`, `waiting`, `needs_verification`, `failed`, `complete` |
| `ReadinessImpact` | does this finding block the action (`blocking`) or is it informative only (`advisory`) — deliberately not named "severity", which usually means low/medium/high |
| `BlockerEvidence` | the concrete fact backing one blocker, for staff review |
| `RepairAction` | the repair the OWNING domain service can execute — `runner` (a dotted path to the real function) is never exposed on the transport schema; a client only ever sees `key`/`action_url` |
| `ActionableBlocker` | one reason the action can't proceed, attributed to its owner |
| `NextAction` | a follow-on action, optionally declaring which blocker code it clears |
| `OperationReference` / `ActionCorrelation` | correlation/causation metadata; field names byte-match `app.services.owner_commands.CommandContext` |
| `ActionReadiness` | the full verdict: state, blockers, next actions, correlation |

### The enforced invariant: a blocker names a real, decision-making owner

`ActionableBlocker.owner` and `NextAction.owner` are validated in
`__post_init__` against the live SOT registry
(`app.services.sot_relationships.service_relationship`, imported lazily to
avoid a cycle, mirroring the existing pattern in
`app.services.owner_commands._validate_manifest`). Two things must both be
true:

1. the name resolves to a registered `SOTService`;
2. that service's contract is **not** `TransactionMode.NOT_APPLICABLE`.

The second check is what makes this an enforced invariant rather than a
convention: a pure-vocabulary contract — including this module's own
registration, `ui.action_readiness_contracts` — can never own a blocker. Only
a service that actually decides something may be named as the reason an
action is blocked.

### State/blocker consistency

- `ready`/`complete` require zero blockers with `impact=blocking`.
- `blocked`/`waiting`/`needs_verification`/`failed` require at least one.
- Blocker codes are unique within one verdict; a `NextAction
  .clears_blocker_code` must name a code that's actually declared.
- `evaluated_at` and `BlockerEvidence.observed_at` must be timezone-aware.
- Every URL-shaped field must be application-relative.

## Rendering

`web_action_readiness.readiness_panel(readiness, audience=...)` is a pure
projection: it selects `customer_message` vs `staff_detail` per row and
derives a `StatusPresentation` from `status_presentation
.action_readiness_presentation(state)` — the same server-owned label/tone/icon
pattern used everywhere else in this codebase (`app/services
/status_presentation.py`). Templates render the resulting `ReadinessPanel`
through the single `action_readiness_panel` macro
(`templates/components/actions/action_readiness.html`), which reuses the
existing `status_presentation_badge` macro for its badge and carries no
`state`/blocker-`code` branching of its own — every audience- or
state-dependent decision is already resolved by the time the template sees
the data.

## First consumer: payment-proof review (this change)

`app.services.web_billing_payment_proofs._review_readiness` translates the
existing `payment_proofs.review_eligibility(...)` verdict into an
`ActionReadiness`, attributed to the payment proof's already-registered SOT
owner, `financial.payment_proofs`. `ActionForm.gated_by(readiness, …)`
(`app/services/action_forms.py`) replaces the literal
`allowed=`/`disabled_reason=` construction at both call sites. The decision
logic in `review_eligibility` is untouched; only the shape that reaches the
form and the new readiness panel changed.
`tests/test_payment_proof_action_forms.py` proves, for every scenario the
existing eligibility suite covers, that the old and new `allowed` verdicts
agree exactly.

## Second consumer: ONT/ACS (separate change)

ONT/ACS provisioning preflight is the primary operational proof (the Jabi
reference case) and is intentionally out of scope for this change — it is
being built in a separate, already-dispatched worktree.

## Explicitly deferred

Extraction of this contract to the `dotmac_starter_mt`/`dotmac-ui` kernel is
a much later step in the plan and is out of scope here. This contract is
built and adopted entirely inside `dotmac_sub` first.
