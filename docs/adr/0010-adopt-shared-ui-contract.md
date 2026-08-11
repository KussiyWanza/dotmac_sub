# ADR-0010: Adopt the shared UI contract, and what the adoption measured

**Status:** Accepted (plumbing) / Proposed (token reconciliation — decision 3)
**Date:** 2026-08-11
**Decision owner:** Michael
**Relates to:** `dotmac_starter_mt` ADR-0006 (white-label product foundation),
ADR-0017 (adoption is the metric), and
`dotmac_starter_mt:docs/inventories/ui-surface-inventory.md`.

## Context

`dotmac-ui` publishes the fleet's shared design tokens. Academy became its
first external consumer on 2026-08-11. Sub was named the second, on the
strength of the starter's UI surface inventory, which found that ERP and Sub
"do not have independent design systems — they have one system, forked once":
an identical 23-file tree under `src/css/`, 3,263 lines with 24% drift, 8 files
byte-identical. The recorded plan was to migrate the token layer, then promote
the 8 identical files, then reconcile the five most-drifted.

Executing that plan against Sub measured two things that change it.

### Finding 1 — Sub's `src/css/` tree is dead code

Nothing builds it. `package.json`'s `css:build` compiles
`static/css/src/main.css`, not `src/css/main.css`. No Dockerfile, workflow,
Makefile or module references `src/css`. The compiled `static/css/main.css`
contains **zero** occurrences of `--ink` and **zero** of `--parchment`, the
tokens that tree defines. Its last commit is 2026-02-16, the commit that added
it; the live tree's is 2026-07-14.

ERP is the opposite: `build:css` compiles `./src/css/main.css` →
`static/css/app.css`, both Dockerfiles copy `src/css`, and the compiled output
carries `--ink` and 35 `parchment` references. Last touched 2026-07-24.

**So the fork is real archaeology but only ERP still runs its copy.** Much of
the measured "drift" is Sub's copy fossilising while ERP's kept moving. This
matters beyond Sub: promoting the 8 byte-identical files into the shared
package would be a **one-consumer extraction**, which ADR-0006 § 5 gate 1
forbids — and it is precisely the "promoting dead CSS" risk the inventory
itself flagged as unsettled. Steps 3 and 4 of the recorded order need re-basing
on the live trees before they proceed.

Sub's live token surface is `static/css/design-system.css` (353 lines) plus the
`@theme` block in `static/css/src/main.css` (Tailwind v4, CSS-first).

### Finding 2 — same role names, different values

Sub's live `design-system.css` already uses `dotmac-ui`'s role vocabulary
verbatim, unprefixed: `--surface-{primary,secondary,tertiary}`,
`--text-{primary,secondary,tertiary}`, `--border-{default,subtle}`,
`--color-semantic-{positive,info,warning,negative,neutral}-*`,
`--status-{surface,border,foreground,indicator}`, `--color-{brand,accent}-*`.
That is not a coincidence — `dotmac-ui`'s `COMPATIBILITY.md` records that the
vocabulary was taken from this file, and `contract.py` explains the `--dmui-`
prefix exists specifically so the package cannot collide with it.

Comparing all 85 by value: **6 identical, 79 different.** The package took
Sub's role *names* and shipped Tailwind's default ramps under them. Sub's brand
is green (`--color-brand-500: #367920`); the package's is blue (`#3b82f6`).
Sub's neutrals are warm (`#596678`); the package's are slate (`#64748b`).

**Adopting the package's values would repaint every Sub page.** The vocabulary
is already shared; only the palette is not.

## Decision

### 1. Sub consumes `dotmac-ui` through its published surface

Exact pin `0.1.0a3` from the private index, the installed asset mounted at the
package's namespace ahead of Sub's catch-all `/static`, and one composition
module (`app/ui.py`) owning the seam. No copy of the compiled CSS enters this
repository; `tests/architecture/test_dotmac_ui_adoption.py` fails the build if
one does.

### 2. No second theme bootstrap

`dotmac_ui.contract.DARK_THEME_SELECTORS` emits dark values under `.dark` as
well as the package attribute, explicitly so Sub's existing class toggle drives
them. `base.html`'s pre-paint script is therefore kept and no packaged
bootstrap is installed. The architecture test pins the dependency: if the
package ever drops `.dark`, that test fails rather than dark mode silently
half-working.

### 3. The shared stylesheet loads FIRST, and nothing is re-valued yet

`_dotmac_ui_head.html` is included before `main.css`, `design-system.css` and
the runtime `theme.css`, so Sub's own values keep winning by source order.
**This adoption changes no pixel.** That is deliberate: it makes Sub a real
consumer — proving mount, pin, digest URL and theme hook — without coupling
that to the palette question, which is a design decision and not a migration.

The palette question, left open here: does Sub

- **(a)** keep its palette and re-declare the `--dmui-*` ramps with Sub's
  values — the override path `COMPATIBILITY.md` sanctions ("a runtime brand
  override re-declares the ramp once and every role follows"), giving one
  vocabulary, one owner, zero visual change; or
- **(b)** adopt the package's ramps, giving one vocabulary *and* one palette
  across the fleet, at the cost of repainting Sub?

(a) is the conservative default and the one this ADR assumes if nothing further
is decided. (b) is a product design decision that belongs to Michael, not to an
adoption task.

## Consequences

- Sub is the second `dotmac-ui` consumer, which is what ADR-0006's extraction
  gate requires before the package's token/theme contract counts as proven.
- The eight public templates under `templates/public/` still load Tailwind from
  `cdn.tailwindcss.com` and do not participate in this adoption. That is a
  separate defect (an external CDN on customer-facing pages) and is not widened
  into this change.
- `src/css/` remains in the tree, unbuilt. Deleting it is a follow-up worth
  doing — dead code that looks live is what made the fleet inventory read Sub
  as an active consumer of a design system it abandoned in February.
- The starter's inventory and its recorded step order need updating with
  finding 1 before steps 3 and 4 run.
