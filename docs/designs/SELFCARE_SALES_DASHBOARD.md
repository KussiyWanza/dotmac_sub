# Selfcare Sales Dashboard

## Page contract

The administrative sales dashboard at `/admin/sales` is a read-only projection
for staff with `crm:lead:read`. It reproduces the operational layout of the
former CRM sales dashboard while following the Selfcare administration design
system.

The header exposes three navigation actions:

- `More` contains Leads, Quotes, and Sales Orders.
- `Pipeline Board` opens the native lead Kanban board.
- `Pipeline Settings` opens native pipeline administration.

The page offers an optional pipeline filter and fixed 7, 30, 90, and 180-day
periods. It shows pipeline value, weighted value, open deals, win rate, average
deal size, pipeline by stage, six-month revenue forecast, agent performance,
and the ten most recently updated opportunities.

## Ownership and calculation contract

`sales.service` remains authoritative for native Lead, Pipeline, PipelineStage,
and Quote state. `app.services.sales.reports` is its read-only reporting
projection and is the only implementation of dashboard calculations. The web
route and Jinja templates are adapters and perform no reporting calculations.

The reporting projection reads request-time database state and performs no
writes. Pipeline totals use active open opportunities created in the selected
period. Closed-deal results use `closed_at` in that period. Forecast values use
the next six calendar months of active, non-closed opportunities with an
expected close date. Recent opportunities are pipeline-scoped but intentionally
not period-scoped, matching the operational worklist contract.

Currency is never summed across codes. Each aggregate retains its currency
groups; the display projection renders explicit currency codes and the forecast
creates separate expected and weighted series per currency.

Agent identity is currently retained on Lead as an observed UUID without a
native staff relationship. The dashboard therefore displays a stable shortened
identifier and does not invent agent names or activity counts. A future reviewed
identity binding may enrich the label without changing the performance facts.

## State contract

- Loading: the page keeps a stable-height dashboard region while the read
  projection is requested through HTMX.
- Empty: metric cards remain visible, followed by section-specific explanations
  and a suggestion to widen the filters.
- Error: the partial returns an explicit unavailable state and retry action; it
  never substitutes unknown values with zero.
- Freshness: every load and retry reads current committed Selfcare sales state.

No CRM client, CRM fallback, or outbound CRM write is part of this page.
