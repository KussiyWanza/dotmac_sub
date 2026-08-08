# Document Discount Report

Status: approved by the requester on 2026-08-07.

## Decision

The administrative UI exposes Invoice and Quote discount history from one
read-only report at `/admin/reports/discounts`. The Reports hub is the only
navigation entry. The previous operational URLs redirect to the corresponding
report tab so saved links remain usable, but their templates and context
builders are retired.

`ui.document_discount_report` owns the typed page projection. It delegates
Invoice facts to `financial.invoice_discounts` and Quote facts to
`sales.quote_discount_reporting`; it does not query or reinterpret their
tables. Currency and timestamp display come from `ui.display_formatting`, and
document lifecycle labels come from `ui.status_presentation`.

The existing `/admin/reports/custom-pricing` page is named **Custom Pricing**.
It describes subscription unit-price overrides and active add-ons, not Invoice
or Quote discount evidence.

## Double-count rule

Invoice and Quote history remain separate tabs and separate record counts.
An Invoice history row whose source is `quote` is labeled **Inherited from
quote** and links to that source Quote. The report never adds that Invoice
amount to the source Quote amount as a new combined discount.

The report does not sum history revisions. Applied, changed, removed, and
inherited rows are immutable evidence, not independent current discounts; a
money total across revisions would overstate the commercial concession even
before Quote inheritance is considered. Each row therefore shows its exact
recorded financial effect, while each tab reports only its matching history
entry count.

## Page contract

- Screen: `reports-discounts` at `/admin/reports/discounts`; page type: report
  history list.
- Audience/job: administrators with `reports:billing:read` periodically review
  who granted or changed document discounts and the recorded financial effect.
- Decision supported: audit one discount revision, distinguish Quote and
  Invoice evidence, and identify inherited Invoice evidence without double
  counting it.
- Read owner: `ui.document_discount_report`; authoritative inputs:
  `financial.invoice_discounts` and `sales.quote_discount_reporting`.
- First viewport: report identity, Invoice/Quote tabs, the double-count
  explanation, filters, and the beginning of the selected work surface.
- Actions: no state-changing actions. Each row links to its canonical Invoice
  or Quote; inherited Invoice rows also link to the source Quote.
- Columns: document/customer, original subtotal, entered and actual discount,
  discounted subtotal/final total, source/reason, actor/time, action, document
  status, and revision.
- Filters: selected document tab, inclusive date range, customer, actor,
  discount type, document status, and Invoice source. Default ordering is
  newest evidence first with stable identifier tie-breaking; pagination is
  server-owned.
- Money: every row retains and displays its explicit currency. Different
  currencies are never added.
- Freshness: transactional. A committed revision is visible on the next read;
  there is no cache or independently writable projection.
- States: empty filtered results, invalid filters, unavailable reads, and
  unauthorized access are distinct. Read failures state that no data changed.
- Responsive projection: desktop uses a bounded seven-column table; mobile
  uses cards retaining document/customer, amount impact, source, actor/time,
  action, status, and drill-down links.
- Observability: unexpected database failures log bounded diagnostics without
  the customer search text.

## Validation and retirement

Focused tests cover typed delegation for both tabs, source-Quote disclosure,
currency/time formatting, permission gates, redirect compatibility, template
rendering, empty/error states, and the absence of old operational UI links and
templates. The source-of-truth registry and generated relationship map name the
new projection owner.
