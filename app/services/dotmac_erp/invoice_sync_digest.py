"""Sub's canonical content digest over the invoice-accounting-sync.v2 projection.

Sub is the sole owner of the invoice-accounting-sync feed's content-identity
digest (Michael's ruling, 2026-09): a downstream Integrator connector and an
ERP shadow task each independently guessed at a fingerprint algorithm and
disagreed, and one of the two guesses folded the nested subscriber profile
(``account``) into the fingerprint even though a subscriber-profile edit never
advances ``invoice.updated_at`` — producing a false "same revision, different
projection" contradiction. This module is step 1 of that fix: Sub defines
exactly which typed facts are covered and how they are encoded, and computes
the digest itself. Changing the connector and the ERP shadow task to forward
this digest verbatim instead of recomputing their own is separate, not-yet-done
follow-up work (steps 3-5 of the approved plan) in those other repositories —
nothing in this commit changes their behavior.

Reuses this repo's ADR-0064-governed canonicalisation primitives
(``app.migration_source.canonical``) rather than an ad hoc
``json.dumps(sort_keys=True)`` encoder — that ad hoc style is exactly what the
now-superseded connector/ERP fingerprint attempts used, and what ADR-0064
supersedes.

Covered-fact domain (version 1) is every field of ``InvoiceAccountingSyncRead``
EXCEPT:

- the nested ``account``/``InvoiceSyncAccountRead`` object (a full subscriber
  profile — name/email/phone/address/status/category — none of which is an
  invoice fact and none of which advances ``invoice.updated_at``; only the
  flat ``account_id`` UUID reference is covered), and
- ``updated_at`` itself, which is the external revision KEY the digest is
  compared against per-key, not digest content. Including it would make
  digest drift trivially "explained" by the key changing and would add no
  signal about whether the covered facts actually changed.

``digest_version`` is included INSIDE the digested payload. Changing the
covered-fact set or its encoding is an explicit cutover per ADR-0064
("Changing the fingerprint contract is an explicit cutover; stored old
submissions replay stored bytes/version") — bump
``INVOICE_PROJECTION_DIGEST_VERSION`` when that happens. Version 1's meaning
must never change silently: the covered fields and their canonical rendering
listed above are frozen under version 1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

from app.migration_source.canonical import (
    CanonicalField,
    canonical_datetime,
    canonical_decimal,
    canonical_digest,
    canonical_form,
    canonical_string,
    canonical_uuid,
)
from app.schemas.billing import (
    InvoiceAccountingSyncDisposition,
    InvoiceAccountingSyncIssueRead,
    InvoiceAccountingSyncLineRead,
    InvoiceAccountingSyncSourceKind,
)

if TYPE_CHECKING:
    from app.models.billing import Invoice, InvoiceDiscountType

#: A future change to the covered-fact set or its encoding must bump this
#: constant as an explicit cutover (ADR-0064) — version 1's meaning never
#: changes silently.
INVOICE_PROJECTION_DIGEST_VERSION: Final[int] = 1


def _canonical_issue(issue: InvoiceAccountingSyncIssueRead) -> str:
    return canonical_form(
        {
            "code": issue.code.value,
            "line_id": canonical_uuid(issue.line_id),
            "expected_amount": canonical_decimal(issue.expected_amount),
            "actual_amount": canonical_decimal(issue.actual_amount),
        }
    )


def _canonical_line(line: InvoiceAccountingSyncLineRead) -> str:
    return canonical_form(
        {
            "id": canonical_uuid(line.id),
            "description": canonical_string(line.description),
            "quantity": canonical_decimal(line.quantity),
            "unit_price": canonical_decimal(line.unit_price),
            "source_amount": canonical_decimal(line.source_amount),
            "net_amount_before_discount": canonical_decimal(
                line.net_amount_before_discount
            ),
            "tax_amount_before_discount": canonical_decimal(
                line.tax_amount_before_discount
            ),
            "gross_amount_before_discount": canonical_decimal(
                line.gross_amount_before_discount
            ),
            "tax_rate_id": canonical_uuid(line.tax_rate_id),
            "tax_rate_code": canonical_string(line.tax_rate_code),
            "tax_rate_percent": canonical_decimal(line.tax_rate_percent),
            "tax_rate_is_active": line.tax_rate_is_active,
            "tax_application": line.tax_application.value,
        }
    )


def compute_invoice_projection_digest(
    *,
    contract_version: str,
    source_kind: InvoiceAccountingSyncSourceKind,
    invoice: Invoice,
    subtotal_before_discount: Decimal,
    discount_type: InvoiceDiscountType | None,
    discount_amount: Decimal,
    discounted_subtotal: Decimal,
    tax_total: Decimal,
    total: Decimal,
    balance_due: Decimal,
    disposition: InvoiceAccountingSyncDisposition,
    issues: list[InvoiceAccountingSyncIssueRead],
    lines: list[InvoiceAccountingSyncLineRead],
) -> str:
    """SHA-256 hex digest over the version-1 covered-fact domain.

    Takes the already-computed local values ``project_invoice_for_accounting``
    holds in scope rather than the built ``InvoiceAccountingSyncRead`` object,
    to avoid a circular "build the object to digest it, then put the digest
    back in the object" shape. Deliberately excludes the nested ``account``
    object and ``updated_at`` — see the module docstring.
    """

    issue_forms: tuple[str, ...] = tuple(
        sorted(_canonical_issue(issue) for issue in issues)
    )
    line_forms: tuple[str, ...] = tuple(sorted(_canonical_line(line) for line in lines))

    fields: dict[str, CanonicalField] = {
        "digest_version": INVOICE_PROJECTION_DIGEST_VERSION,
        "contract_version": canonical_string(contract_version),
        "source_kind": source_kind.value,
        "source_invoice_id": canonical_uuid(invoice.id),
        "source_splynx_invoice_id": invoice.splynx_invoice_id,
        "account_id": canonical_uuid(invoice.account_id),
        "invoice_number": canonical_string(invoice.invoice_number),
        "status": invoice.status.value,
        "currency": canonical_string(invoice.currency),
        "subtotal_before_discount": canonical_decimal(subtotal_before_discount),
        "discount_type": (None if discount_type is None else discount_type.value),
        "discount_value": canonical_decimal(invoice.discount_value),
        "discount_amount": canonical_decimal(discount_amount),
        "discounted_subtotal": canonical_decimal(discounted_subtotal),
        "tax_total": canonical_decimal(tax_total),
        "total": canonical_decimal(total),
        "balance_due": canonical_decimal(balance_due),
        "issued_at": canonical_datetime(invoice.issued_at),
        "due_at": canonical_datetime(invoice.due_at),
        "paid_at": canonical_datetime(invoice.paid_at),
        "memo": canonical_string(invoice.memo),
        "is_proforma": invoice.is_proforma,
        "disposition": disposition.value,
        "issues": issue_forms,
        "lines": line_forms,
    }
    return canonical_digest(fields)


__all__ = [
    "INVOICE_PROJECTION_DIGEST_VERSION",
    "compute_invoice_projection_digest",
]
