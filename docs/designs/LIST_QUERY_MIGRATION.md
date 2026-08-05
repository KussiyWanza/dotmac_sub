# list_query List Migration

**Goal:** route the migrated admin/reseller lists through `app/services/list_query.py`
so every list has server-side sort/filter/pagination, URL-encoded (deep-linkable)
state, `aria-sort`, deterministic ordering, and keyboard-operable rows — the
Carbon/WCAG-2.2-AA list standard. Each list's KPI tiles then link to their now-
filterable cohorts.

**Reference exemplar:** `app/services/web_customer_lists.py` (definition + build +
apply) and `templates/admin/customers/_table.html` (rendering). Copy those.

**Pattern-setters shipped here:** `templates/components/ui/list_macros.html`
provides `sort_header` — one Carbon/WCAG sortable header from a `ListQuery`
(deep-link, aria-sort, non-colour direction, accessible label) — and
`list_pagination`, which renders the live result count, page-size form, and
navigation entirely from `ListQuery` + `PageMeta`. A migrated table needs no
hand-rolled query strings, header, or pagination markup.

## Per-resource recipe (one validated commit each)

1. **Declare the list** — a `ListDefinition` with `ListFieldDefinition`s marking
   `searchable` / `filterable` / `sortable`, plus `default_sort`. Put it next to
   the resource's read owner (e.g. a `web_referrals` module or the existing
   service).
2. **Build the query** — a `build_*_list_query(**request_params) -> ListQuery`
   that normalizes raw request values through `definition.build_query(...)`
   (which rejects unsupported sort/filter keys rather than silently applying
   them).
3. **Apply + paginate** — apply `list_query`'s sort/filters/offset to the
   resource's SQLAlchemy query, count the filtered total, and build
   `PageMeta.from_query(list_query, total)`. Return rows + `list_query` +
   `page_meta`.
4. **Route** — read the request params, call build + apply, pass
   `list_query`, `page_meta`, and rows to the template. Keep the route a thin
   adapter (no query logic).
5. **Template** — replace hand-rolled headers with
   `{% from "components/ui/list_macros.html" import sort_header %}` +
   `sort_header(list_query, base_url, key, label, entity=...)` per sortable
   column, and replace Prev/Next with the `list_pagination` macro. Make row
   navigation an `<a>` (keyboard-operable), never `onclick`.
6. **KPI tiles** — wire each stat tile to `list_query.url(base_url, filters=...)`
   for its cohort; where the cohort needs a new filter, add it to the
   `ListDefinition` (this is where the deferred KPI-tile links from the
   remediation branch get finished).
7. **Test** — a projection test asserting sort/filter/pagination over more rows
   than one page and that unsupported sort/filter keys are rejected.

## Adopt the projection contracts

As each list is migrated, express its KPI tiles as `ui_contracts.Kpi` (value as
`StateValue`, `cohort_url` = the filtered list) and any row/bulk actions as
`ui_contracts.Action`. See `docs/designs/UI_PROJECTION_CONTRACTS.md`.

## Resource worklist

Admin: **referrals** (pattern-setter, start here) · sales leads · sales quotes ·
sales orders · inbox. Reseller: accounts · invoices · work-orders.

Sales Leads now uses the generic URL/pagination contract together with a typed
`sales.service` query input and outcome. The owner normalizes search and filters
once, and correlated related-record predicates keep the PostgreSQL `json` Lead
row out of any full-row `DISTINCT` operation.

Each is independent; migrate and validate one at a time. The reference
implementation, the two macros, and this recipe make each a mechanical change
rather than a design problem.
