"""Service helpers for billing invoice batch routes."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.models.billing import BillingRun, BillingRunStatus
from app.models.catalog import BillingCycle
from app.services import billing as billing_service
from app.services import billing_automation as billing_automation_service
from app.services import display_format
from app.services.action_forms import (
    ActionConfirmation,
    ActionForm,
    ActionHiddenValue,
    ActionTone,
)
from app.services.domain_errors import DomainError

logger = logging.getLogger(__name__)

INVOICE_BATCH_ERROR_MESSAGE = (
    "Invoice batch could not be completed. Check billing run logs for details."
)
INVOICE_BATCH_PREVIEW_ERROR_MESSAGE = (
    "Invoice batch preview could not be prepared. Check the selected cycle and date."
)
INVOICE_BATCH_STALE_MESSAGE = (
    "The billable subscription scope changed after preview. Review the updated "
    "impact before confirming again."
)


@dataclass(frozen=True, slots=True)
class InvoiceBatchSubscriptionImpact:
    subscription_id: str
    account_id: str
    offer_name: str
    amount: Decimal
    currency: str
    period_start: str
    period_end: str
    pending_activation: bool

    @property
    def amount_display(self) -> str:
        return display_format.format_money(self.amount, currency=self.currency)


@dataclass(frozen=True, slots=True)
class InvoiceBatchPreview:
    billing_cycle: str | None
    billing_date: str
    source_run_id: str | None
    invoice_count: int
    account_count: int
    subscription_count: int
    skipped_count: int
    totals_by_currency: tuple[tuple[str, Decimal], ...]
    subscriptions: tuple[InvoiceBatchSubscriptionImpact, ...]
    fingerprint: str

    @property
    def total_display(self) -> str:
        return display_format.format_currency_groups(dict(self.totals_by_currency))


class InvoiceBatchActionError(DomainError):
    """Safe staff batch action error."""


def _get_billing_run(db, run_id: str) -> BillingRun:
    try:
        parsed_id = UUID(run_id)
    except (TypeError, ValueError) as exc:
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.run_not_found",
            message="Billing run not found.",
            details={"run_id": run_id},
        ) from exc
    run = db.get(BillingRun, parsed_id)
    if run is None:
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.run_not_found",
            message="Billing run not found.",
            details={"run_id": run_id},
        )
    return run


def parse_billing_cycle(value: str | None) -> BillingCycle | None:
    if not value:
        return None
    try:
        return BillingCycle(value)
    except ValueError as exc:
        raise ValueError("Invalid billing cycle") from exc


def _parse_run_date(billing_date: str | None) -> datetime | None:
    if not billing_date:
        return None
    return datetime.strptime(billing_date, "%Y-%m-%d").replace(tzinfo=UTC)


def _preview_fingerprint(
    *,
    billing_cycle: str | None,
    billing_date: str,
    source_run_id: str | None,
    invoice_count: int,
    account_count: int,
    skipped_count: int,
    totals_by_currency: tuple[tuple[str, Decimal], ...],
    subscriptions: tuple[InvoiceBatchSubscriptionImpact, ...],
) -> str:
    payload = {
        "billing_cycle": billing_cycle,
        "billing_date": billing_date,
        "source_run_id": source_run_id,
        "invoice_count": invoice_count,
        "account_count": account_count,
        "skipped_count": skipped_count,
        "totals_by_currency": [
            [currency, str(amount)] for currency, amount in totals_by_currency
        ],
        "subscriptions": [
            {
                "subscription_id": row.subscription_id,
                "account_id": row.account_id,
                "offer_name": row.offer_name,
                "amount": str(row.amount),
                "currency": row.currency,
                "period_start": row.period_start,
                "period_end": row.period_end,
                "pending_activation": row.pending_activation,
            }
            for row in subscriptions
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preview_batch_action(
    db,
    *,
    billing_cycle: str | None,
    billing_date: str | None,
    source_run_id: str | None = None,
    parse_cycle_fn: Callable[[str | None], Any] = parse_billing_cycle,
) -> InvoiceBatchPreview:
    """Build a deterministic, side-effect-free postpaid invoice batch preview."""
    run_date = _parse_run_date(billing_date) or datetime.now(UTC)
    normalized_date = run_date.date().isoformat()
    parsed_cycle = parse_cycle_fn(billing_cycle)
    normalized_cycle = (
        parsed_cycle.value if hasattr(parsed_cycle, "value") else billing_cycle or None
    )

    summary = billing_automation_service.run_invoice_cycle(
        db=db,
        billing_cycle=parsed_cycle,
        dry_run=True,
        run_at=run_date,
        run_prepaid_renewals=False,
    )
    raw_subscriptions = summary.get("subscriptions", [])
    subscriptions = tuple(
        sorted(
            (
                InvoiceBatchSubscriptionImpact(
                    subscription_id=str(row.get("id") or ""),
                    account_id=str(row.get("account_id") or ""),
                    offer_name=str(row.get("offer_name") or "Unknown"),
                    amount=Decimal(str(row.get("amount") or "0.00")),
                    currency=str(row.get("currency") or "NGN").upper(),
                    period_start=str(row.get("period_start") or ""),
                    period_end=str(row.get("period_end") or ""),
                    pending_activation=bool(row.get("pending_activation")),
                )
                for row in raw_subscriptions
                if isinstance(row, dict)
            ),
            key=lambda row: row.subscription_id,
        )
    )
    raw_totals = summary.get("totals_by_currency", {})
    totals_by_currency = tuple(
        sorted(
            (
                str(currency).upper(),
                Decimal(str(amount)),
            )
            for currency, amount in (
                raw_totals.items() if isinstance(raw_totals, dict) else ()
            )
        )
    )
    invoice_count = int(summary.get("invoices_created", 0) or 0)
    account_count = int(summary.get("accounts_affected", 0) or 0)
    skipped_count = int(summary.get("skipped", 0) or 0)
    return InvoiceBatchPreview(
        billing_cycle=normalized_cycle,
        billing_date=normalized_date,
        source_run_id=source_run_id,
        invoice_count=invoice_count,
        account_count=account_count,
        subscription_count=len(subscriptions),
        skipped_count=skipped_count,
        totals_by_currency=totals_by_currency,
        subscriptions=subscriptions,
        fingerprint=_preview_fingerprint(
            billing_cycle=normalized_cycle,
            billing_date=normalized_date,
            source_run_id=source_run_id,
            invoice_count=invoice_count,
            account_count=account_count,
            skipped_count=skipped_count,
            totals_by_currency=totals_by_currency,
            subscriptions=subscriptions,
        ),
    )


def build_batch_action_form(preview: InvoiceBatchPreview) -> ActionForm:
    """Project the shared confirmation form for one exact batch preview."""
    allowed = preview.subscription_count > 0
    source_note = (
        f" This is a new run based on failed run {preview.source_run_id}."
        if preview.source_run_id
        else ""
    )
    return ActionForm(
        key="invoice_batch.run",
        title="Confirm invoice batch",
        description=(
            "Generate postpaid invoices for the exact billable scope shown above."
            f"{source_note}"
        ),
        action_url="/admin/billing/invoices/generate-batch/confirm",
        submit_label="Run invoice batch",
        fields=(),
        tone=ActionTone.positive,
        impact=(
            f"{preview.invoice_count} invoice(s) for "
            f"{preview.subscription_count} subscription(s) across "
            f"{preview.account_count} account(s), totaling {preview.total_display}."
        ),
        confirmation=ActionConfirmation(
            title="Run this reviewed invoice batch",
            message=(
                "This creates customer-visible invoice and ledger documents. "
                "The scope is rechecked before execution."
            ),
        ),
        hidden_values=tuple(
            ActionHiddenValue(key=key, value=value)
            for key, value in (
                ("billing_cycle", preview.billing_cycle or "all"),
                ("billing_date", preview.billing_date),
                ("preview_fingerprint", preview.fingerprint),
                ("source_run_id", preview.source_run_id or "manual"),
            )
        ),
        allowed=allowed,
        disabled_reason=(
            None
            if allowed
            else "No subscriptions are currently eligible for this invoice batch."
        ),
    )


def preview_retry_batch(db, *, run_id: str) -> InvoiceBatchPreview:
    """Preview a new run only from one failed historical run."""
    run = _get_billing_run(db, run_id)
    if run.status is not BillingRunStatus.failed:
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.retry_ineligible",
            message="Only a failed billing run can be reviewed for retry.",
            details={"run_id": run_id},
        )
    return preview_batch_action(
        db,
        billing_cycle=run.billing_cycle or None,
        billing_date=datetime.now(UTC).date().isoformat(),
        source_run_id=str(run.id),
    )


def confirm_batch_action(
    db,
    *,
    billing_cycle: str | None,
    billing_date: str | None,
    preview_fingerprint: str,
    source_run_id: str | None,
    confirmed: bool,
    actor: str,
    parse_cycle_fn: Callable[[str | None], Any] = parse_billing_cycle,
) -> str:
    """Revalidate the preview and start the authoritative invoice cycle."""
    if not confirmed:
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.confirmation_required",
            message="Explicit confirmation is required.",
        )
    if not actor.strip():
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.invalid_actor",
            message="Authorized staff identity is required.",
        )
    normalized_cycle = None if billing_cycle in {None, "", "all"} else billing_cycle
    normalized_source = None if source_run_id in {None, "", "manual"} else source_run_id
    if normalized_source:
        source = _get_billing_run(db, normalized_source)
        if source.status is not BillingRunStatus.failed:
            raise InvoiceBatchActionError(
                code="financial.invoice_batch_staff_actions.retry_ineligible",
                message="The source billing run is no longer eligible for retry.",
                details={"run_id": normalized_source},
            )
    current = preview_batch_action(
        db,
        billing_cycle=normalized_cycle,
        billing_date=billing_date,
        source_run_id=normalized_source,
        parse_cycle_fn=parse_cycle_fn,
    )
    if not hmac.compare_digest(preview_fingerprint, current.fingerprint):
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.stale_preview",
            message=INVOICE_BATCH_STALE_MESSAGE,
        )
    if current.subscription_count == 0:
        raise InvoiceBatchActionError(
            code="financial.invoice_batch_staff_actions.empty_scope",
            message="No subscriptions are currently eligible for this invoice batch.",
        )
    summary = billing_automation_service.run_invoice_cycle(
        db=db,
        billing_cycle=parse_cycle_fn(normalized_cycle),
        dry_run=False,
        run_at=_parse_run_date(current.billing_date),
        run_prepaid_renewals=False,
        launch_kind="retry" if normalized_source else "manual",
        requested_by=actor,
        preview_fingerprint=current.fingerprint,
        source_run_id=UUID(normalized_source) if normalized_source else None,
    )
    run_at = summary.get("run_at")
    run_at_text = (
        run_at.strftime("%Y-%m-%d") if isinstance(run_at, datetime) else "today"
    )
    return (
        f"Batch run completed for {run_at_text}. "
        f"Invoices created: {summary.get('invoices_created', 0)} · "
        f"Subscriptions billed: {summary.get('subscriptions_billed', 0)} · "
        f"Skipped: {summary.get('skipped', 0)}."
    )


def _status_badge(status: BillingRunStatus | str | None) -> str:
    if isinstance(status, BillingRunStatus):
        status_key = status.value
    else:
        status_key = str(status or "")
    return {
        "success": "success",
        "failed": "danger",
        "running": "warning",
    }.get(status_key, "neutral")


def _run_status_text(status: BillingRunStatus | str | None) -> str:
    if isinstance(status, BillingRunStatus):
        return status.value.title()
    return str(status or "unknown").replace("_", " ").title()


def list_recent_runs(db, *, limit: int = 20) -> list[dict[str, object]]:
    runs = billing_service.billing_runs.list(
        db=db,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    return [
        {
            "id": str(run.id),
            "run_at": run.run_at,
            "created_at": run.created_at,
            "billing_cycle": run.billing_cycle or "all",
            "launch_kind": run.launch_kind or "scheduled",
            "source_run_id": str(run.source_run_id) if run.source_run_id else None,
            "requested_by": run.requested_by,
            "subscriptions_scanned": int(run.subscriptions_scanned or 0),
            "subscriptions_billed": int(run.subscriptions_billed or 0),
            "invoices_created": int(run.invoices_created or 0),
            "lines_created": int(run.lines_created or 0),
            "skipped": int(run.skipped or 0),
            "status": _run_status_text(run.status),
            "status_badge": _status_badge(run.status),
            "retry_allowed": run.status is BillingRunStatus.failed,
            "retry_reason": (
                None
                if run.status is BillingRunStatus.failed
                else "Only failed billing runs can be reviewed for retry."
            ),
            "status_message": (
                run.error
                if run.error
                else (
                    "Transactions have been created"
                    if _run_status_text(run.status) == "Success"
                    else (
                        "Run is currently processing"
                        if _run_status_text(run.status) == "Running"
                        else "—"
                    )
                )
            ),
            "error": run.error,
            "duration_seconds": (
                int((run.finished_at - run.started_at).total_seconds())
                if run.finished_at and run.started_at
                else None
            ),
        }
        for run in runs
    ]


def get_run_row(db, *, run_id: str) -> dict[str, object] | None:
    run = billing_service.billing_runs.get(db, run_id)
    if not run:
        return None
    status_text = _run_status_text(run.status)
    return {
        "id": str(run.id),
        "run_at": run.run_at,
        "created_at": run.created_at,
        "billing_cycle": run.billing_cycle or "all",
        "launch_kind": run.launch_kind or "scheduled",
        "source_run_id": str(run.source_run_id) if run.source_run_id else None,
        "requested_by": run.requested_by,
        "subscriptions_scanned": int(run.subscriptions_scanned or 0),
        "subscriptions_billed": int(run.subscriptions_billed or 0),
        "invoices_created": int(run.invoices_created or 0),
        "lines_created": int(run.lines_created or 0),
        "skipped": int(run.skipped or 0),
        "status": status_text,
        "status_badge": _status_badge(run.status),
        "retry_allowed": run.status is BillingRunStatus.failed,
        "retry_reason": (
            None
            if run.status is BillingRunStatus.failed
            else "Only failed billing runs can be reviewed for retry."
        ),
        "status_message": (
            run.error
            if run.error
            else (
                "Transactions have been created"
                if status_text == "Success"
                else (
                    "Run is currently processing" if status_text == "Running" else "—"
                )
            )
        ),
        "error": run.error,
        "duration_seconds": (
            int((run.finished_at - run.started_at).total_seconds())
            if run.finished_at and run.started_at
            else None
        ),
    }


def render_runs_csv(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "run_id",
            "run_at",
            "created_at",
            "billing_cycle",
            "launch_kind",
            "source_run_id",
            "requested_by",
            "subscriptions_scanned",
            "subscriptions_billed",
            "invoices_created",
            "lines_created",
            "skipped",
            "status",
            "status_message",
            "duration_seconds",
            "error",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("id", ""),
                row.get("run_at").isoformat() if row.get("run_at") else "",
                row.get("created_at").isoformat() if row.get("created_at") else "",
                row.get("billing_cycle", ""),
                row.get("launch_kind", ""),
                row.get("source_run_id", ""),
                row.get("requested_by", ""),
                row.get("subscriptions_scanned", 0),
                row.get("subscriptions_billed", 0),
                row.get("invoices_created", 0),
                row.get("lines_created", 0),
                row.get("skipped", 0),
                row.get("status", ""),
                row.get("status_message", ""),
                row.get("duration_seconds", ""),
                row.get("error", ""),
            ]
        )
    return buffer.getvalue()


def render_single_run_csv(row: dict[str, object]) -> str:
    return render_runs_csv([row])


def build_batch_page_state(
    db,
    *,
    note: str | None = None,
    error: str | None = None,
    preview: InvoiceBatchPreview | None = None,
    batch_action_form: ActionForm | None = None,
    can_write: bool = False,
) -> dict[str, object]:
    """Build invoice batch page state."""
    from app.services import (
        web_billing_invoice_actions as web_billing_invoice_actions_service,
    )

    return {
        "today": web_billing_invoice_actions_service.batch_today_str(),
        "recent_runs": list_recent_runs(db, limit=25),
        "note": note,
        "error": error,
        "batch_preview": preview,
        "batch_action_form": batch_action_form,
        "can_write": can_write,
    }
