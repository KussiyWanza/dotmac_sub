"""Online payment provider flows for customer portal."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import UploadFile
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentMethodType,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services import gateway_topup_intents
from app.services.account_credit_deposits import (
    SETTLEMENT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    ActiveDepositRequest,
    SettleAccountCreditDepositCommand,
)
from app.services.billing import collection_account_directory
from app.services.billing._common import lock_account
from app.services.billing.account_credit import eligible_invoices
from app.services.billing_adapter import PaymentIntent, billing_adapter
from app.services.collections import get_available_balance, restore_account_services
from app.services.common import round_money, to_decimal
from app.services.customer_context import (
    customer_can_access_account,
    optional_customer_account_id,
    optional_customer_subscriber_id,
    require_customer_account_id,
)
from app.services.customer_portal_context import (
    get_invoice_billing_contact,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import payment_capability
from app.services.integrations.runtime_execution import RuntimeExecutionError
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import (
    PaymentGatewayContext,
    payment_gateway_adapter,
)
from app.services.payment_routing import (
    GatewayOption,
    gateway_options,
    provider_for_intent,
    select_checkout_provider,
)
from app.services.provider_payment_settlements import (
    settle_verified_invoice_payment,
)
from app.services.settings_spec import resolve_value
from app.services.topup_intents import (
    COMPLETION_SCOPE,
    DIRECT_TRANSFER_PROVIDER,
    CompleteTopupIntentCommand,
    DirectTransferAccountMapping,
    DirectTransferAdapterSettings,
    DirectTransferBankAccountEvidence,
    TopupIntentCompletionSource,
    TopupIntentStatus,
    direct_transfer_configuration,
    stage_topup_intent_completion,
)

logger = logging.getLogger(__name__)
_ONLINE_PROVIDER_LABELS = {
    "paystack": "Pay with Paystack",
    "flutterwave": "Pay with Flutterwave",
}
_DIRECT_TRANSFER_LABEL = "Direct bank transfer"
_DEFAULT_TOPUP_PRESET_AMOUNTS = (1000, 2000, 5000, 10000, 20000, 50000)


def _serialize_deposit_preview(preview) -> dict[str, object]:
    return {
        "account_id": str(preview.account_id),
        "currency": preview.currency,
        "current_account_credit": preview.current_account_credit,
        "requested_deposit": preview.requested_deposit,
        "eligible_invoice_count": preview.eligible_invoice_count,
        "invoice_applications": [
            {
                "invoice_id": str(item.invoice_id),
                "invoice_number": item.invoice_number,
                "currency": item.currency,
                "amount_applied": item.amount_applied,
                "outstanding_after_application": item.outstanding_after_application,
            }
            for item in preview.invoice_applications
        ],
        "total_applied_to_invoices": preview.total_applied_to_invoices,
        "total_outstanding_after_application": (
            preview.total_outstanding_after_application
        ),
        "remaining_account_credit": preview.remaining_account_credit,
        "projected_available_credit": preview.projected_available_credit,
        "allocation_policy": preview.allocation_policy,
        "credit_application_policy": preview.credit_application_policy,
        "policy_version": preview.policy_version,
        "preview_fingerprint": preview.fingerprint,
    }


def _serialize_active_deposit_request(
    request: ActiveDepositRequest,
) -> dict[str, object]:
    return {
        "intent_id": str(request.intent_id),
        "phase": request.phase.value,
        "next_action": request.next_action.value,
        "provider_type": request.provider_type,
        "reference": request.reference,
        "amount": request.amount,
        "currency": request.currency,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "observed_at": request.observed_at,
        "rejection_reason": request.rejection_reason,
    }


def _provider_uuid(db: Session, provider_type: str) -> uuid.UUID | None:
    """Resolve the PaymentProvider row id for a gateway type.

    Stamping provider_id on verify-path payments is what lets the webhook
    ingest path (and the (provider_id, external_id) unique index) recognise
    the same gateway transaction and refuse to credit it twice.
    """
    try:
        provider = billing_service.payment_providers.get_by_type(
            db, PaymentProviderType(provider_type)
        )
    except ValueError:
        return None
    return provider.id if provider else None


def _payment_by_gateway_identity(
    db: Session,
    *,
    external_id: str,
    provider_id: uuid.UUID | None,
) -> Payment | None:
    query = select(Payment).where(Payment.external_id == external_id)
    if provider_id is not None:
        query = query.where(
            or_(Payment.provider_id == provider_id, Payment.provider_id.is_(None))
        ).order_by((Payment.provider_id == provider_id).desc())
    return db.scalars(query).first()


def online_gateway_payment_options(db: Session) -> list[dict[str, str]]:
    """Return healthy gateways in canonical routing order."""
    return [
        {
            "provider_type": route.provider_type.value,
            "label": _ONLINE_PROVIDER_LABELS[route.provider_type.value],
        }
        for route in gateway_options(db)
    ]


def _default_online_route(db: Session):
    try:
        return select_checkout_provider(db)
    except ValueError:
        return None


def _runtime_ready_gateway_contexts(
    db: Session,
    *,
    invoice_number: str | None = None,
) -> list[tuple[GatewayOption, PaymentGatewayContext]]:
    """Map runtime-unavailable gateways to an honest UI availability state."""

    contexts: list[tuple[GatewayOption, PaymentGatewayContext]] = []
    for route in gateway_options(db):
        try:
            context = payment_gateway_adapter.build_context(
                db,
                provider_type=route.provider_type.value,
                capability_binding_id=route.capability_binding_id,
                invoice_number=invoice_number,
            )
        except (
            RuntimeExecutionError,
            payment_capability.PaymentCapabilityError,
        ) as exc:
            logger.warning(
                "Payment gateway unavailable for customer presentment",
                extra={
                    "event": "payment_gateway_presentment_unavailable",
                    "provider_type": route.provider_type.value,
                    "capability_binding_id": str(route.capability_binding_id),
                    "error_type": type(exc).__name__,
                },
            )
            continue
        contexts.append((route, context))
    return contexts


def _payment_options_for_runtime_ready_routes(
    routes: list[tuple[GatewayOption, PaymentGatewayContext]],
    *,
    direct_transfer_enabled: bool,
) -> list[dict[str, str]]:
    options = [
        {
            "provider_type": route.provider_type.value,
            "label": _ONLINE_PROVIDER_LABELS[route.provider_type.value],
        }
        for route, _context in routes
    ]
    if direct_transfer_enabled:
        options.append(
            {
                "provider_type": DIRECT_TRANSFER_PROVIDER,
                "label": _DIRECT_TRANSFER_LABEL,
            }
        )
    return options


def _topup_payment_options(
    db: Session,
    *,
    direct_transfer_enabled: bool | None = None,
) -> list[dict[str, str]]:
    """Return online provider options for customer payments.

    Online gateways come from :func:`online_gateway_payment_options`; direct
    bank transfer is appended when configured.

    Pass ``direct_transfer_enabled`` when the caller has already resolved it to
    avoid re-running the bank-transfer settings query.
    """
    options = online_gateway_payment_options(db)
    if direct_transfer_enabled is None:
        direct_transfer_enabled = direct_bank_transfer_enabled(db)
    if direct_transfer_enabled:
        options.append(
            {
                "provider_type": DIRECT_TRANSFER_PROVIDER,
                "label": _DIRECT_TRANSFER_LABEL,
            }
        )
    return options


def direct_bank_transfer_settings(db: Session) -> DirectTransferAdapterSettings:
    """Serialize the canonical transfer configuration for legacy adapters."""

    return direct_transfer_configuration(db).to_adapter_settings()


def enabled_direct_bank_transfer_accounts(
    db: Session,
) -> list[DirectTransferAccountMapping]:
    return collection_account_directory.enabled_transfer_accounts(db)


def direct_bank_transfer_enabled(db: Session) -> bool:
    return direct_transfer_configuration(db).enabled


def _resolve_topup_limits(db: Session) -> tuple[int, int]:
    """Return minimum and maximum allowed top-up amounts."""
    min_amount = resolve_value(db, SettingDomain.billing, "topup_min_amount")
    max_amount = resolve_value(db, SettingDomain.billing, "topup_max_amount")
    min_amount_value = (
        int(min_amount) if isinstance(min_amount, (str, int, float)) else 1000
    )
    max_amount_value = (
        int(max_amount) if isinstance(max_amount, (str, int, float)) else 500000
    )
    return min_amount_value, max_amount_value


def _default_topup_presets(min_amount: int, max_amount: int) -> list[int]:
    return [
        amount
        for amount in _DEFAULT_TOPUP_PRESET_AMOUNTS
        if min_amount <= amount <= max_amount
    ]


def _resolve_topup_presets(
    db: Session,
    *,
    min_amount: int,
    max_amount: int,
) -> list[int]:
    """Return configured top-up presets constrained by the active limits."""
    raw_presets = resolve_value(db, SettingDomain.billing, "topup_preset_amounts")
    if not isinstance(raw_presets, str) or not raw_presets.strip():
        return _default_topup_presets(min_amount, max_amount)

    presets: list[int] = []
    seen: set[int] = set()
    for part in raw_presets.split(","):
        try:
            amount = int(part.strip())
        except ValueError:
            return _default_topup_presets(min_amount, max_amount)
        if amount <= 0:
            return _default_topup_presets(min_amount, max_amount)
        if min_amount <= amount <= max_amount and amount not in seen:
            presets.append(amount)
            seen.add(amount)

    return presets or _default_topup_presets(min_amount, max_amount)


def _format_naira(amount: Decimal | int | float) -> str:
    rounded = round_money(to_decimal(amount))
    return f"₦{rounded:,.2f}"


def _customer_account_uuid(db: Session, customer: dict) -> uuid.UUID:
    account_id = require_customer_account_id(db, customer)
    return uuid.UUID(str(account_id))


def _topup_policy_warnings(intent: TopupIntent) -> list[str]:
    metadata = dict(intent.metadata_ or {})
    violations = list(metadata.get("policy_violations") or [])
    requested_amount = round_money(to_decimal(metadata.get("requested_amount") or 0))
    actual_amount = round_money(
        to_decimal(metadata.get("actual_amount") or requested_amount or 0)
    )
    warnings: list[str] = []
    if "amount_mismatch" in violations:
        warnings.append(
            "The amount confirmed by the payment provider differed from the amount requested at checkout."
        )
    if "amount_below_min" in violations:
        warnings.append(
            f"The confirmed amount was below the usual minimum add-funds amount of {_format_naira(metadata.get('min_amount') or 0)}."
        )
    if "amount_above_max" in violations:
        warnings.append(
            f"The confirmed amount was above the usual maximum add-funds amount of {_format_naira(metadata.get('max_amount') or 0)}."
        )
    if "intent_expired" in violations:
        warnings.append(
            "The payment completed after the original checkout session had expired."
        )
    if warnings and requested_amount and actual_amount:
        warnings.insert(
            0,
            f"Requested {_format_naira(requested_amount)} but the provider confirmed {_format_naira(actual_amount)}.",
        )
    return warnings


def _build_legacy_topup_policy_violations(
    *,
    requested_amount: Decimal,
    actual_amount: Decimal,
    min_amount: int,
    max_amount: int,
    expires_at: datetime | None,
) -> list[str]:
    """Preserve the policy evidence contract for pre-migration intents."""
    violations: list[str] = []
    if actual_amount != requested_amount:
        violations.append("amount_mismatch")
    if actual_amount < Decimal(str(min_amount)):
        violations.append("amount_below_min")
    if actual_amount > Decimal(str(max_amount)):
        violations.append("amount_above_max")
    normalized_expires_at = expires_at
    if normalized_expires_at and normalized_expires_at.tzinfo is None:
        normalized_expires_at = normalized_expires_at.replace(tzinfo=UTC)
    if normalized_expires_at and normalized_expires_at < datetime.now(UTC):
        violations.append("intent_expired")
    return violations


def _finalize_legacy_topup_intent(
    db: Session,
    intent: TopupIntent,
    *,
    payment: Payment,
    actual_amount: Decimal,
    policy_violations: list[str],
    min_amount: int,
    max_amount: int,
) -> None:
    metadata = dict(intent.metadata_ or {})
    metadata.update(
        {
            "requested_amount": str(intent.requested_amount),
            "actual_amount": str(actual_amount),
            "min_amount": min_amount,
            "max_amount": max_amount,
            "policy_violations": policy_violations,
        }
    )
    intent.metadata_ = metadata
    db.add(intent)
    stage_topup_intent_completion(
        db,
        CompleteTopupIntentCommand(
            intent_id=intent.id,
            payment_id=payment.id,
            source=TopupIntentCompletionSource.customer_legacy_topup_verify,
        ),
        context=CommandContext.system(
            actor=f"customer:{intent.account_id}",
            scope=COMPLETION_SCOPE,
            reason="Project verified legacy payment onto top-up intent",
        ),
    )
    db.commit()
    db.refresh(intent)


def _retry_legacy_topup_restore(db: Session, account_id: uuid.UUID) -> None:
    """Keep the old consequence only for already-issued, untyped intents."""
    try:
        from app.services.billing.reconcile_unposted import (
            settle_prepaid_draft_invoices_from_credit,
        )

        settled = settle_prepaid_draft_invoices_from_credit(db, str(account_id))
        if settled.changed:
            db.commit()
        restore_account_services(db, str(account_id))
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Legacy top-up restore retry failed for account %s: %s",
            account_id,
            exc,
        )


def _verify_and_record_legacy_topup(
    db: Session,
    *,
    intent: TopupIntent,
    reference: str,
    provider_type: str,
) -> dict:
    """Settle a pre-migration intent without silently changing its policy."""
    assert intent.account_id is not None
    provider_id = _coerce_uuid_or_none(
        (intent.metadata_ or {}).get("provider_id")
    ) or _provider_uuid(db, provider_type)
    lock_account(db, str(intent.account_id))
    db.refresh(intent)
    if intent.completed_payment_id:
        completed_payment = db.get(Payment, intent.completed_payment_id)
        if (
            completed_payment is None
            or completed_payment.account_id != intent.account_id
        ):
            raise ValueError("Recorded top-up payment could not be found")
        _retry_legacy_topup_restore(db, intent.account_id)
        return _build_topup_result(
            db,
            payment=completed_payment,
            intent=intent,
            amount=round_money(to_decimal(completed_payment.amount or 0)),
            reference=reference,
            already_recorded=True,
        )

    transaction = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
        capability_binding_id=(
            str(intent.capability_binding_id) if intent.capability_binding_id else None
        ),
    )
    amount = round_money(transaction.amount)
    metadata_intent_id = str((transaction.metadata or {}).get("topup_intent_id") or "")
    if metadata_intent_id and metadata_intent_id != str(intent.id):
        raise ValueError("Verified payment did not match the original checkout session")
    min_amount, max_amount = _resolve_topup_limits(db)
    policy_violations = _build_legacy_topup_policy_violations(
        requested_amount=round_money(intent.requested_amount),
        actual_amount=amount,
        min_amount=min_amount,
        max_amount=max_amount,
        expires_at=intent.expires_at,
    )
    existing = _payment_by_gateway_identity(
        db,
        external_id=transaction.external_id,
        provider_id=provider_id,
    )
    if existing is not None:
        if existing.account_id != intent.account_id:
            raise ValueError("Payment reference is linked to a different account")
        payment = existing
        already_recorded = True
    else:
        try:
            payment = billing_adapter.record_payment(
                db,
                PaymentIntent(
                    account_id=intent.account_id,
                    amount=amount,
                    currency=transaction.currency,
                    status=PaymentStatus.succeeded,
                    provider_id=provider_id,
                    external_id=transaction.external_id,
                    memo=(
                        f"{transaction.memo_prefix} legacy prepaid top-up ref: "
                        f"{reference}"
                    ),
                    allocations=[],
                ),
            )
            already_recorded = False
        except IntegrityError:
            db.rollback()
            payment = _payment_by_gateway_identity(
                db,
                external_id=transaction.external_id,
                provider_id=provider_id,
            )
            if payment is None or payment.account_id != intent.account_id:
                raise
            already_recorded = True

    _finalize_legacy_topup_intent(
        db,
        intent,
        payment=payment,
        actual_amount=amount,
        policy_violations=policy_violations,
        min_amount=min_amount,
        max_amount=max_amount,
    )
    from app.services.events import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.usage_topped_up,
        {
            "account_id": str(intent.account_id),
            "amount": str(amount),
            "reference": reference,
            "legacy_intent": True,
        },
        account_id=intent.account_id,
    )
    _retry_legacy_topup_restore(db, intent.account_id)
    return _build_topup_result(
        db,
        payment=payment,
        intent=intent,
        amount=amount,
        reference=reference,
        already_recorded=already_recorded,
    )


def _build_topup_result(
    db: Session,
    *,
    payment: Payment,
    intent: TopupIntent,
    amount: Decimal,
    reference: str,
    already_recorded: bool,
) -> dict:
    return {
        "payment": payment,
        "amount": amount,
        "reference": reference,
        "provider_type": intent.provider_type,
        "already_recorded": already_recorded,
        "policy_warnings": _topup_policy_warnings(intent),
        **_build_topup_summary(db, payment),
    }


def _build_topup_summary(db: Session, payment: Payment) -> dict:
    """Describe how a top-up was allocated and what credit remains."""
    allocations = db.scalars(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == payment.id,
            PaymentAllocation.is_active.is_(True),
        )
    ).all()

    invoice_ids = [allocation.invoice_id for allocation in allocations]
    invoices_by_id: dict[str, Invoice] = {}
    if invoice_ids:
        invoices = db.scalars(select(Invoice).where(Invoice.id.in_(invoice_ids))).all()
        invoices_by_id = {str(invoice.id): invoice for invoice in invoices}

    allocated_to_invoices: list[dict[str, object]] = []
    total_allocated = Decimal("0.00")
    for allocation in allocations:
        amount = round_money(to_decimal(getattr(allocation, "amount", 0) or 0))
        total_allocated += amount
        invoice = invoices_by_id.get(str(allocation.invoice_id))
        allocated_to_invoices.append(
            {
                "invoice_id": str(allocation.invoice_id),
                "invoice_number": getattr(invoice, "invoice_number", None),
                "amount": amount,
            }
        )

    total_allocated = round_money(total_allocated)
    application = billing_service.payments.application_summary(db, payment)
    # The payment owner distinguishes gross cash received from customer value
    # credited after provider fees. Never display the fee as retained credit.
    credit_added = application.unallocated_credit

    from app.models.catalog import CatalogOffer, Subscription
    from app.services.prepaid_service_renewals import renewal_outcomes_for_payment

    renewed_services: list[dict[str, object]] = []
    for outcome in renewal_outcomes_for_payment(db, payment.id):
        subscription = db.get(Subscription, outcome.subscription_id)
        offer = (
            db.get(CatalogOffer, subscription.offer_id)
            if subscription is not None
            else None
        )
        renewed_services.append(
            {
                "subscription_id": str(outcome.subscription_id),
                "offer_name": getattr(offer, "name", None) or "Service",
                "amount": outcome.amount,
                "renewed_through": outcome.renewed_through,
            }
        )

    available_balance: Decimal | None = None
    try:
        available_balance = round_money(
            get_available_balance(db, str(payment.account_id))
        )
    except Exception:
        logger.warning(
            "Failed to resolve available balance after top-up for account %s",
            payment.account_id,
            exc_info=True,
        )

    return {
        "allocated_to_invoices": allocated_to_invoices,
        "allocated_total": total_allocated,
        "amount_credited": application.amount_credited,
        "prepaid_amount_applied": application.prepaid_amount_applied,
        "credit_added": credit_added,
        "available_balance": available_balance,
        "renewed_services": renewed_services,
    }


def get_payment_page(
    db: Session,
    customer: dict,
    invoice_id: str,
) -> dict | None:
    """Build context for the online payment page."""
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        return None

    if invoice.status in (
        InvoiceStatus.paid,
        InvoiceStatus.void,
        InvoiceStatus.written_off,
    ):
        return None

    billing_contact = get_invoice_billing_contact(db, invoice, customer)
    email = billing_contact["billing_email"] or _resolve_customer_email(db, customer)

    account_id = getattr(invoice, "account_id", None)
    payment_methods = []
    if account_id:
        try:
            payment_methods = customer_cards.list_for_account(db, str(account_id))
        except Exception:
            logger.warning(
                "Failed to resolve payment methods for account %s",
                account_id,
                exc_info=True,
            )

    amount_due = round_money(
        to_decimal(
            getattr(invoice, "balance_due", None) or getattr(invoice, "total", 0) or 0
        )
    )

    # The web chooser mints a per-provider reference/checkout via the intent
    # endpoint (mirroring the top-up flow). The bearer API
    # (``initiate_payment``) instead consumes a single pre-minted gateway
    # context, so keep ``provider_public_key``/``payment_reference`` for it.
    dbt_enabled = direct_bank_transfer_enabled(db)
    runtime_ready_gateways = _runtime_ready_gateway_contexts(
        db,
        invoice_number=getattr(invoice, "invoice_number", None),
    )
    gateway_context = runtime_ready_gateways[0][1] if runtime_ready_gateways else None
    return {
        "invoice": invoice,
        "amount": amount_due,
        "provider_type": (
            gateway_context.provider_type
            if gateway_context
            else DIRECT_TRANSFER_PROVIDER
            if dbt_enabled
            else None
        ),
        "provider_public_key": gateway_context.public_key if gateway_context else None,
        "paystack_public_key": gateway_context.public_key
        if gateway_context and gateway_context.provider_type == "paystack"
        else None,
        "payment_reference": gateway_context.reference if gateway_context else None,
        "payment_options": _payment_options_for_runtime_ready_routes(
            runtime_ready_gateways,
            direct_transfer_enabled=dbt_enabled,
        ),
        "payment_methods": payment_methods,
        "direct_bank_transfer_enabled": dbt_enabled,
        "customer_email": email,
    }


_INVOICE_CHARGE_IDEMPOTENCY_SCOPE = "invoice_saved_card_charge"


def _invoice_charge_replay(reference: str) -> dict:
    """Return checkout context for a replayed saved-card invoice charge.

    The card was already charged on the original request, so the replay points
    the client straight at verification rather than charging again."""
    return {
        "provider_type": "paystack",
        "provider_public_key": None,
        "reference": reference,
        "charged": True,
        "checkout_url": None,
        "replayed": True,
    }


def _reserve_charge_idempotency_key(
    db: Session,
    *,
    scope: str,
    key: str,
    account_id: uuid.UUID,
    replay,
) -> tuple["IdempotencyKey | None", dict | None]:
    """Reserve an idempotency key before a server-side card charge.

    Returns ``(reservation, replay_result)``. If ``replay_result`` is not None
    the card was already charged on a prior request — the caller MUST return it
    immediately and not charge again. Otherwise ``reservation`` is a freshly
    committed row whose ``ref_id`` the caller sets after a successful charge
    (:func:`_commit_charge_idempotency_ref`) or releases on failure
    the failure command owner releases atomically with the failed intent.
    ``replay(ref_id)`` maps a stored ref_id to a replay payload, or None when
    the prior attempt left no usable result (then the stale key is dropped and
    re-reserved).
    """
    prior = db.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == key,
        )
    ).first()
    if prior is not None:
        if str(prior.account_id) != str(account_id):
            raise ValueError("Idempotency key already used")
        replayed = replay(prior.ref_id) if prior.ref_id else None
        if replayed is not None:
            return None, replayed
        db.delete(prior)
        db.commit()
    reservation = IdempotencyKey(
        scope=scope,
        key=key,
        account_id=account_id,
        ref_id=None,
    )
    db.add(reservation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.scalars(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == scope,
                IdempotencyKey.key == key,
            )
        ).first()
        replayed = replay(prior.ref_id) if (prior and prior.ref_id) else None
        if replayed is not None:
            return None, replayed
        raise ValueError("A payment with this key is already in progress.")
    return reservation, None


def _commit_charge_idempotency_ref(
    db: Session, reservation: "IdempotencyKey | None", ref_id: str
) -> None:
    """Bind a successful charge's ref to its reserved key so replays are safe."""
    if reservation is not None:
        reservation.ref_id = ref_id
        db.add(reservation)
        db.commit()


def _init_flutterwave_checkout(
    db: Session,
    customer: dict,
    *,
    amount: Decimal,
    reference: str,
    redirect_url: str | None,
    metadata: dict,
    default_callback_path: str,
    capability_binding_id: uuid.UUID,
    currency: str | None = None,
) -> str:
    """Start a Flutterwave hosted checkout and return its link.

    Shared by the top-up and invoice-pay flows; they differ only in
    ``default_callback_path`` (the verify route to return to).
    """
    from app.services.integrations import payment_capability

    callback_url = redirect_url or default_callback_path
    if callback_url.startswith("/"):
        # Flutterwave requires an absolute redirect_url; a relative path breaks
        # the hosted-checkout return leg (mobile hits this branch).
        from app.services.email import _get_app_url

        base_url = _get_app_url(db) or ""
        if base_url:
            callback_url = f"{base_url}{callback_url}"
    separator = "&" if "?" in callback_url else "?"
    try:
        checkout = payment_capability.initialize_transaction(
            db,
            provider_type="flutterwave",
            email=_resolve_customer_email(db, customer),
            amount=amount,
            reference=reference,
            redirect_url=(
                f"{callback_url}{separator}reference={reference}&provider=flutterwave"
            ),
            metadata=metadata,
            currency=currency,
            checkout_binding_id=capability_binding_id,
        )
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("Flutterwave checkout initialization failed", exc_info=True)
        raise ValueError(
            "Unable to start Flutterwave checkout. Check Flutterwave configuration and try again."
        ) from exc
    link = checkout.get("link")
    if not link:
        logger.warning(
            "Flutterwave checkout initialization returned no link: %s", checkout
        )
        raise ValueError("Flutterwave did not return a checkout link")
    return link


def _charge_saved_card_for_invoice(
    db: Session,
    customer: dict,
    *,
    invoice: Invoice,
    amount: Decimal,
    payment_method_id: str,
    checkout_metadata: dict,
    provider_id: uuid.UUID,
    capability_binding_id: uuid.UUID,
    idempotency_key: str | None,
) -> dict:
    """Charge a saved card server-side for an invoice (Paystack only).

    Recording/allocation happens in :func:`verify_and_record_payment` when the
    client returns to the verify route — the gateway metadata carries the
    ``invoice_id``. An ``idempotency_key`` makes the charge safe against
    double-submit: a replay returns the original reference rather than charging
    the card a second time."""
    account_id = uuid.UUID(str(invoice.account_id))
    method = customer_cards._owned(db, str(account_id), payment_method_id)
    if method is None:
        raise ValueError("Payment method not found")
    token = billing_service.payment_methods.get_decrypted_token(db, str(method.id))
    if not token:
        raise ValueError("Payment method is not chargeable")

    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type="paystack",
        capability_binding_id=capability_binding_id,
        invoice_number=getattr(invoice, "invoice_number", None),
    )
    reference = gateway_context.reference

    # Reserve the idempotency key BEFORE charging so a concurrent (or replayed)
    # same-key request returns the original reference instead of charging twice.
    idem_key = (idempotency_key or "").strip() or None
    reservation: IdempotencyKey | None = None
    if idem_key:
        reservation, replayed = _reserve_charge_idempotency_key(
            db,
            scope=_INVOICE_CHARGE_IDEMPOTENCY_SCOPE,
            key=idem_key,
            account_id=account_id,
            replay=lambda ref_id: _invoice_charge_replay(ref_id) if ref_id else None,
        )
        if replayed is not None:
            return replayed

    reservation_id = reservation.id if reservation is not None else None
    created_by = str(optional_customer_subscriber_id(db, customer) or account_id)
    create_command = gateway_topup_intents.CreateCustomerGatewayTopupIntentCommand(
        flow=gateway_topup_intents.CustomerGatewayTopupFlow.invoice_payment,
        account_id=account_id,
        invoice_id=invoice.id,
        reference=reference,
        provider_type=gateway_context.provider_type,
        provider_id=provider_id,
        capability_binding_id=capability_binding_id,
        payment_method_id=method.id,
        created_by=created_by,
    )
    db_session_adapter.release_read_transaction(db)
    intent_result = gateway_topup_intents.create_customer_gateway_topup_intent(
        db,
        create_command,
        context=CommandContext.system(
            actor=f"customer:{created_by}",
            scope=gateway_topup_intents.CREATE_CUSTOMER_SCOPE,
            reason="Customer saved-card invoice checkout intent",
            idempotency_key=idem_key,
        ),
    )

    from app.services.integrations import payment_capability

    try:
        payment_capability.charge_authorization(
            db,
            authorization_code=token,
            email=_resolve_customer_email(db, customer),
            amount_kobo=payment_capability.amount_to_kobo(amount),
            reference=reference,
            metadata=checkout_metadata,
            checkout_binding_id=capability_binding_id,
        )
    except Exception:
        db_session_adapter.release_read_transaction(db)
        gateway_topup_intents.fail_saved_card_charge(
            db,
            gateway_topup_intents.FailSavedCardChargeCommand(
                intent_id=intent_result.intent_id,
                reservation_id=reservation_id,
                reservation_scope=gateway_topup_intents.SavedCardChargeScope.invoice,
            ),
            context=CommandContext.system(
                actor=f"customer:{created_by}",
                scope=gateway_topup_intents.FAIL_SAVED_CARD_SCOPE,
                reason="Record failed saved-card invoice charge",
            ),
        )
        raise
    _commit_charge_idempotency_ref(db, reservation, reference)

    return {
        "provider_type": "paystack",
        "provider_public_key": gateway_context.public_key,
        "reference": reference,
        "amount": intent_result.requested_amount,
        "currency": intent_result.currency,
        "checkout_metadata": checkout_metadata,
        "charged": True,
        "checkout_url": None,
    }


def create_invoice_payment_intent(
    db: Session,
    customer: dict,
    invoice_id: str,
    *,
    provider: str | None = None,
    payment_method_id: str | None = None,
    redirect_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Start an invoice payment via the customer's chosen method.

    Mirrors :func:`create_topup_intent` but settles a specific invoice:

    * a **saved card** is charged server-side (Paystack only);
    * a **gateway** choice (Paystack inline / Flutterwave hosted) returns
      checkout context for the client to open;
    * a **bank transfer** hands off to the direct-transfer proof flow.

    The amount is the invoice balance (server-authoritative — the client cannot
    set it). The verified payment is allocated to ``invoice_id`` by
    :func:`verify_and_record_payment`, which reads ``invoice_id`` back from the
    gateway metadata.
    """
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        raise ValueError("Invoice not found or access denied")
    if (
        invoice.status
        in (
            InvoiceStatus.draft,
            InvoiceStatus.paid,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        )
        or invoice.is_proforma
    ):
        raise ValueError("Invoice is no longer payable")

    amount = round_money(
        to_decimal(
            getattr(invoice, "balance_due", None) or getattr(invoice, "total", 0) or 0
        )
    )
    if amount <= Decimal("0.00"):
        raise ValueError("Invoice no longer has an outstanding balance")

    # Bank transfer: reuse the direct-transfer proof flow, prefilled with the
    # invoice balance and tagged with the invoice so the proof is traceable.
    # Limits are NOT enforced here (a real invoice may be below the top-up
    # minimum, e.g. a small reconnection fee). The reviewed transfer credits the
    # account and auto-allocation settles outstanding invoices oldest-first.
    if provider == DIRECT_TRANSFER_PROVIDER:
        return create_direct_transfer_topup_intent(
            db,
            customer,
            amount,
            invoice_id=str(invoice.id),
            idempotency_key=idempotency_key,
        )

    selected_payment_method_id = str(payment_method_id or "").strip() or None
    route = select_checkout_provider(
        db,
        "paystack" if selected_payment_method_id else provider,
    )
    provider_type = route.provider_type.value
    customer_email = _resolve_customer_email(db, customer)
    _require_gateway_email(provider_type, customer_email)

    invoice_number = getattr(invoice, "invoice_number", None)
    checkout_metadata = {
        "payment_flow": "invoice_payment",
        "invoice_id": str(invoice.id),
        "invoice_number": invoice_number or "",
        "account_id": str(invoice.account_id),
        "provider_id": str(route.provider_id),
    }

    # Saved card -> server-to-server Paystack charge.
    if selected_payment_method_id:
        if provider_type != "paystack":
            raise ValueError("Saved cards can only be used with Paystack")
        return _charge_saved_card_for_invoice(
            db,
            customer,
            invoice=invoice,
            amount=amount,
            payment_method_id=selected_payment_method_id,
            checkout_metadata=checkout_metadata,
            provider_id=route.provider_id,
            capability_binding_id=route.capability_binding_id,
            idempotency_key=idempotency_key,
        )

    # Gateway checkout (Paystack inline opened client-side, or Flutterwave
    # hosted checkout we initialize here).
    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
        capability_binding_id=route.capability_binding_id,
        invoice_number=invoice_number,
    )
    # Durable, expirable trace of the started checkout, mirroring the TopupIntent
    # the top-up flow always creates — so a hosted checkout that debits the
    # customer but never returns is reconcilable. Completed in
    # verify_and_record_payment's caller via complete_invoice_payment_intent.
    created_by = str(
        optional_customer_subscriber_id(db, customer) or invoice.account_id
    )
    create_command = gateway_topup_intents.CreateCustomerGatewayTopupIntentCommand(
        flow=gateway_topup_intents.CustomerGatewayTopupFlow.invoice_payment,
        account_id=uuid.UUID(str(invoice.account_id)),
        invoice_id=invoice.id,
        reference=gateway_context.reference,
        provider_type=gateway_context.provider_type,
        provider_id=route.provider_id,
        capability_binding_id=route.capability_binding_id,
        created_by=created_by,
    )
    db_session_adapter.release_read_transaction(db)
    intent_result = gateway_topup_intents.create_customer_gateway_topup_intent(
        db,
        create_command,
        context=CommandContext.system(
            actor=f"customer:{created_by}",
            scope=gateway_topup_intents.CREATE_CUSTOMER_SCOPE,
            reason="Customer invoice gateway checkout intent",
            idempotency_key=(str(idempotency_key or "").strip() or None),
        ),
    )
    result = {
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "reference": gateway_context.reference,
        "amount": intent_result.requested_amount,
        "currency": intent_result.currency,
        "invoice_number": invoice_number,
        "customer_email": customer_email,
        "checkout_metadata": checkout_metadata,
        "charged": False,
        "checkout_url": None,
    }
    if gateway_context.provider_type == "flutterwave":
        result["checkout_url"] = _init_flutterwave_checkout(
            db,
            customer,
            amount=intent_result.requested_amount,
            reference=gateway_context.reference,
            redirect_url=redirect_url,
            metadata=checkout_metadata,
            default_callback_path="/portal/billing/pay/verify",
            capability_binding_id=route.capability_binding_id,
            currency=intent_result.currency,
        )
    return result


def complete_invoice_payment_intent(
    db: Session, reference: str, payment: Payment | None
) -> None:
    """Best-effort: mark a pending invoice checkout record completed after verify.

    No-op when the reference has no invoice checkout record (bearer API and
    legacy references may have none)."""
    try:
        intent = db.scalars(
            select(TopupIntent).where(TopupIntent.reference == reference)
        ).first()
        if intent is None or payment is None:
            return
        if str((intent.metadata_ or {}).get("payment_flow")) != "invoice_payment":
            return
        stage_topup_intent_completion(
            db,
            CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=payment.id,
                source=TopupIntentCompletionSource.customer_invoice_verify,
            ),
            context=CommandContext.system(
                actor=f"customer:{intent.account_id}",
                scope=COMPLETION_SCOPE,
                reason="Project verified invoice payment onto checkout intent",
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "Failed to complete invoice checkout intent for %s",
            reference,
            exc_info=True,
        )


def verify_and_record_payment(
    db: Session,
    customer: dict,
    reference: str,
    *,
    provider: str | None = None,
) -> dict:
    """Verify an online payment transaction and record the payment."""
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()
    if (
        intent is None
        or str((intent.metadata_ or {}).get("payment_flow")) != "invoice_payment"
    ):
        raise ValueError("Payment reference was not issued for this invoice checkout")
    if not customer_can_access_account(db, customer, intent.account_id):
        raise ValueError("Payment reference does not belong to this account")
    provider_type = provider_for_intent(intent, provider).value

    tx = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
        capability_binding_id=(
            str(intent.capability_binding_id) if intent.capability_binding_id else None
        ),
    )
    invoice_id = tx.metadata.get("invoice_id")
    expected_invoice_id = str((intent.metadata_ or {}).get("invoice_id") or "")
    amount_naira = round_money(tx.amount)

    if not invoice_id:
        raise ValueError("Payment metadata missing invoice_id")
    if expected_invoice_id and str(invoice_id) != expected_invoice_id:
        raise ValueError("Verified payment did not match the original invoice checkout")

    provider_id = _coerce_uuid_or_none(
        (intent.metadata_ or {}).get("provider_id")
    ) or _provider_uuid(db, provider_type)
    if provider_id is None:
        raise ValueError("Payment provider configuration is unavailable")

    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        raise ValueError("Invoice not found or access denied")

    settlement = settle_verified_invoice_payment(
        db,
        account_id=uuid.UUID(str(invoice.account_id)),
        invoice_id=uuid.UUID(str(invoice_id)),
        topup_intent_id=intent.id,
        provider_id=provider_id,
        provider_reference=reference,
        external_id=tx.external_id,
        gross_amount=amount_naira,
        provider_fee=round_money(to_decimal(getattr(tx, "provider_fee", 0))),
        net_amount=round_money(to_decimal(intent.requested_amount)),
        currency=tx.currency,
        memo=f"{tx.memo_prefix} payment ref: {reference}",
    )
    payment = settlement.payment
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    summary = _build_topup_summary(db, payment)
    complete_invoice_payment_intent(db, reference, payment)

    return {
        "payment": payment,
        "invoice": invoice,
        "amount": amount_naira,
        "reference": reference,
        "provider_type": provider_type,
        "already_recorded": not settlement.payment_created,
        "allocation_exception_id": (
            str(settlement.reconciliation_exception.id)
            if settlement.reconciliation_exception
            else None
        ),
        **summary,
    }


def _resolve_customer_email(db: Session, customer: dict) -> str:
    """Resolve a real email address for the customer (for payment gateways).

    The session ``username`` is the RADIUS/PPPoE login (or an impersonation
    token), not an email, so Paystack rejects it. Prefer an email already on the
    session, then fall back to the subscriber record. Returns "" if none.
    """
    for candidate in (customer.get("email"), customer.get("billing_email")):
        value = str(candidate or "").strip()
        if "@" in value:
            return value
    account_id = optional_customer_account_id(db, customer)
    if account_id:
        try:
            subscriber = db.get(Subscriber, uuid.UUID(str(account_id)))
        except (ValueError, TypeError):
            subscriber = None
        if subscriber:
            value = str(getattr(subscriber, "email", "") or "").strip()
            if "@" in value:
                return value
    return ""


def _require_gateway_email(provider_type: str, email: str) -> None:
    if provider_type != DIRECT_TRANSFER_PROVIDER and not email:
        raise ValueError(
            "A valid customer email address is required before starting card payment."
        )


def get_topup_page(
    db: Session,
    customer: dict,
) -> dict:
    """Build context for the customer top-up page."""
    account_id = optional_customer_account_id(db, customer)
    direct_transfer_available = direct_bank_transfer_enabled(db)
    runtime_ready_gateways = _runtime_ready_gateway_contexts(db)
    gateway_context = runtime_ready_gateways[0][1] if runtime_ready_gateways else None
    provider_type = (
        gateway_context.provider_type
        if gateway_context
        else DIRECT_TRANSFER_PROVIDER
        if direct_transfer_available
        else None
    )

    # Resolve current balance
    prepaid_balance: Decimal | None = None
    try:
        prepaid_balance = round_money(get_available_balance(db, str(account_id)))
    except Exception:
        logger.warning(
            "Failed to resolve prepaid balance for account %s",
            account_id,
            exc_info=True,
        )

    min_amount_value, max_amount_value = _resolve_topup_limits(db)

    email = _resolve_customer_email(db, customer)
    payment_methods = []
    if account_id:
        try:
            payment_methods = customer_cards.list_for_account(db, str(account_id))
        except Exception:
            logger.warning(
                "Failed to resolve payment methods for account %s",
                account_id,
                exc_info=True,
            )

    payable_invoices = eligible_invoices(db, str(account_id)) if account_id else []
    active_deposit_request = (
        AccountCreditDeposits.active_request(
            db,
            account_id=uuid.UUID(str(account_id)),
        )
        if account_id
        else None
    )
    context = {
        "provider_type": provider_type,
        "payment_options": _payment_options_for_runtime_ready_routes(
            runtime_ready_gateways,
            direct_transfer_enabled=direct_transfer_available,
        ),
        "customer_email": email,
        "prepaid_balance": prepaid_balance,
        "account_credit": prepaid_balance,
        "eligible_unpaid_invoices": [
            {
                "id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "balance_due": round_money(invoice.balance_due),
                "currency": invoice.currency,
            }
            for invoice in payable_invoices
        ],
        "eligible_unpaid_total": round_money(
            sum(
                (to_decimal(invoice.balance_due) for invoice in payable_invoices),
                Decimal("0.00"),
            )
        ),
        "deposit_allowed": active_deposit_request is None,
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "preset_amounts": _resolve_topup_presets(
            db,
            min_amount=min_amount_value,
            max_amount=max_amount_value,
        ),
        "payment_methods": payment_methods,
    }
    if active_deposit_request:
        context["active_deposit_request"] = _serialize_active_deposit_request(
            active_deposit_request
        )

    if gateway_context:
        context["provider_public_key"] = gateway_context.public_key
        if gateway_context.provider_type == "paystack":
            context["paystack_public_key"] = gateway_context.public_key
    else:
        context["provider_public_key"] = None

    return context


def preview_topup(
    db: Session,
    customer: dict,
    amount: Decimal | int | float | str,
) -> dict[str, object]:
    account_id = _customer_account_uuid(db, customer)
    min_amount_value, max_amount_value = _resolve_topup_limits(db)
    preview = AccountCreditDeposits.preview(
        db,
        account_id=account_id,
        amount=amount,
        currency="NGN",
        minimum=min_amount_value,
        maximum=max_amount_value,
    )
    return _serialize_deposit_preview(preview)


def get_payment_methods_page(
    db: Session,
    customer: dict,
) -> dict:
    """Build context for the customer payment-methods management page.

    Surfaces saved cards (with their default flag), the prepaid balance, and the
    direct-bank-transfer details so transfer is a first-class, discoverable
    method rather than a radio buried inside the top-up flow. Autopay status is
    layered on by the route (mirrors the top-up page)."""
    account_id = optional_customer_account_id(db, customer)

    cards = []
    if account_id:
        try:
            cards = customer_cards.list_for_account(db, str(account_id))
        except Exception:
            logger.warning(
                "Failed to resolve payment methods for account %s",
                account_id,
                exc_info=True,
            )
    # Only card-type methods are managed here; bank accounts (if ever stored)
    # are a separate concept and shouldn't appear as "saved cards".
    saved_cards = [c for c in cards if c.method_type == PaymentMethodType.card]

    prepaid_balance: Decimal | None = None
    if account_id:
        try:
            prepaid_balance = round_money(get_available_balance(db, str(account_id)))
        except Exception:
            logger.warning(
                "Failed to resolve prepaid balance for account %s",
                account_id,
                exc_info=True,
            )

    min_amount_value, max_amount_value = _resolve_topup_limits(db)
    default_route = _default_online_route(db)

    return {
        "saved_cards": saved_cards,
        "prepaid_balance": prepaid_balance,
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "provider_type": (
            default_route.provider_type.value
            if default_route
            else DIRECT_TRANSFER_PROVIDER
        ),
        "direct_bank_transfer_enabled": direct_bank_transfer_enabled(db),
        "bank_transfer": direct_bank_transfer_settings(db),
    }


_TOPUP_CHARGE_IDEMPOTENCY_SCOPE = "topup_saved_card_charge"


def _topup_intent_replay(db: Session, ref_id: str | None) -> dict | None:
    """Return the prior saved-card top-up intent for a replayed idempotency key.

    The card was already charged on the original request, so the replay points
    the client straight at verification rather than charging again."""
    intent = db.get(TopupIntent, _coerce_uuid_or_none(ref_id)) if ref_id else None
    if intent is None:
        return None
    checkout_metadata = (
        {"topup_intent_id": str(intent.id)}
        if intent.purpose == "account_credit_deposit"
        else dict(intent.metadata_ or {})
    )
    return {
        "intent_id": str(intent.id),
        "provider_type": intent.provider_type,
        "provider_public_key": None,
        "reference": intent.reference,
        "requested_amount": intent.requested_amount,
        "currency": intent.currency,
        "checkout_metadata": checkout_metadata,
        "charged": True,
        "checkout_url": None,
        "replayed": True,
    }


def _coerce_uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def create_topup_intent(
    db: Session,
    customer: dict,
    amount: Decimal | int | float | str,
    *,
    provider: str | None = None,
    payment_method_id: str | None = None,
    preview_fingerprint: str | None = None,
    redirect_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a server-owned Deposit Account Credit intent for checkout.

    When ``payment_method_id`` selects a saved card the customer's card is
    charged server-side; passing ``idempotency_key`` makes that charge safe
    against double-submit (a replay returns the original intent rather than
    charging the card a second time)."""
    account_id = _customer_account_uuid(db, customer)
    requested_amount = round_money(to_decimal(amount))
    if provider == DIRECT_TRANSFER_PROVIDER:
        return create_direct_transfer_topup_intent(
            db,
            customer,
            requested_amount,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        )

    selected_payment_method_id = str(payment_method_id or "").strip() or None
    route = select_checkout_provider(
        db,
        "paystack" if selected_payment_method_id else provider,
    )
    provider_type = route.provider_type.value

    customer_email = _resolve_customer_email(db, customer)
    _require_gateway_email(provider_type, customer_email)

    selected_payment_method = None
    selected_payment_token = None
    if selected_payment_method_id:
        if provider_type != "paystack":
            raise ValueError("Saved cards can only be used with Paystack")
        selected_payment_method = customer_cards._owned(
            db, str(account_id), selected_payment_method_id
        )
        if selected_payment_method is None:
            raise ValueError("Payment method not found")
        selected_payment_token = billing_service.payment_methods.get_decrypted_token(
            db, str(selected_payment_method.id)
        )
        if not selected_payment_token:
            raise ValueError("Payment method is not chargeable")
    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
        capability_binding_id=route.capability_binding_id,
    )

    # Saved-card charges hit the card server-side, so they need double-submit
    # protection. Gateway-redirect flows are already deduped by the unique
    # gateway reference and need no key. Reserve the key BEFORE charging so a
    # concurrent same-key request fails the unique constraint here.
    idem_key = (idempotency_key or "").strip() or None
    reservation: IdempotencyKey | None = None
    if idem_key and selected_payment_method is not None:
        reservation, replayed = _reserve_charge_idempotency_key(
            db,
            scope=_TOPUP_CHARGE_IDEMPOTENCY_SCOPE,
            key=idem_key,
            account_id=account_id,
            replay=lambda ref_id: _topup_intent_replay(db, ref_id),
        )
        if replayed is not None:
            return replayed

    reservation_id = reservation.id if reservation is not None else None
    created_by = str(optional_customer_subscriber_id(db, customer) or account_id)
    create_command = gateway_topup_intents.CreateCustomerGatewayTopupIntentCommand(
        flow=gateway_topup_intents.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=account_id,
        requested_amount=requested_amount,
        reference=gateway_context.reference,
        provider_type=gateway_context.provider_type,
        provider_id=route.provider_id,
        capability_binding_id=route.capability_binding_id,
        payment_method_id=(
            selected_payment_method.id if selected_payment_method is not None else None
        ),
        created_by=created_by,
        expected_preview_fingerprint=preview_fingerprint,
    )
    db_session_adapter.release_read_transaction(db)
    intent_result = gateway_topup_intents.create_customer_gateway_topup_intent(
        db,
        create_command,
        context=CommandContext.system(
            actor=f"customer:{created_by}",
            scope=gateway_topup_intents.CREATE_CUSTOMER_SCOPE,
            reason="Customer account-credit gateway checkout intent",
            idempotency_key=idem_key,
        ),
    )

    # Provider metadata is deliberately opaque. Settlement reloads every amount,
    # account and policy field from Sub using this reference.
    checkout_metadata = {"topup_intent_id": str(intent_result.intent_id)}
    charged = False
    if selected_payment_method is not None:
        from app.services.integrations import payment_capability

        try:
            payment_capability.charge_authorization(
                db,
                authorization_code=selected_payment_token,
                email=customer_email,
                amount_kobo=payment_capability.amount_to_kobo(
                    intent_result.requested_amount
                ),
                reference=intent_result.reference,
                metadata=checkout_metadata,
                checkout_binding_id=route.capability_binding_id,
            )
        except Exception:
            db_session_adapter.release_read_transaction(db)
            gateway_topup_intents.fail_saved_card_charge(
                db,
                gateway_topup_intents.FailSavedCardChargeCommand(
                    intent_id=intent_result.intent_id,
                    reservation_id=reservation_id,
                    reservation_scope=(
                        gateway_topup_intents.SavedCardChargeScope.account_credit_deposit
                    ),
                ),
                context=CommandContext.system(
                    actor=f"customer:{created_by}",
                    scope=gateway_topup_intents.FAIL_SAVED_CARD_SCOPE,
                    reason="Record failed saved-card account-credit charge",
                ),
            )
            raise
        charged = True
        _commit_charge_idempotency_ref(db, reservation, str(intent_result.intent_id))

    checkout_url = None
    if gateway_context.provider_type == "flutterwave":
        checkout_url = _init_flutterwave_checkout(
            db,
            customer,
            amount=intent_result.requested_amount,
            reference=intent_result.reference,
            redirect_url=redirect_url,
            metadata=checkout_metadata,
            default_callback_path="/portal/billing/topup/verify",
            capability_binding_id=route.capability_binding_id,
        )

    return {
        "intent_id": str(intent_result.intent_id),
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "reference": gateway_context.reference,
        "requested_amount": intent_result.requested_amount,
        "currency": intent_result.currency,
        "preview_fingerprint": intent_result.preview_fingerprint,
        "checkout_metadata": checkout_metadata,
        "charged": charged,
        "checkout_url": checkout_url,
    }


def _latest_pending_direct_transfer_intent(
    db: Session, account_id: uuid.UUID
) -> TopupIntent | None:
    return db.scalars(
        select(TopupIntent)
        .where(TopupIntent.account_id == account_id)
        .where(TopupIntent.provider_type == DIRECT_TRANSFER_PROVIDER)
        .where(TopupIntent.status == TopupIntentStatus.pending.value)
        .order_by(TopupIntent.created_at.desc())
    ).first()


def create_direct_transfer_topup_intent(
    db: Session,
    customer: dict,
    amount: Decimal | int | float | str,
    *,
    preview_fingerprint: str | None = None,
    invoice_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Adapt a customer request to the typed direct-transfer command owner."""

    account_id = _customer_account_uuid(db, customer)
    created_by = str(optional_customer_subscriber_id(db, customer) or account_id)
    from app.services import direct_transfer_intents

    command = direct_transfer_intents.CreateDirectTransferIntentCommand(
        account_id=account_id,
        created_by=created_by,
        requested_amount=amount if invoice_id is None else None,
        invoice_id=uuid.UUID(invoice_id) if invoice_id else None,
        expected_preview_fingerprint=preview_fingerprint,
    )
    context = CommandContext.system(
        actor=f"customer:{created_by}",
        scope=direct_transfer_intents.CREATE_SCOPE,
        reason=(
            "Customer portal invoice direct-transfer intent"
            if invoice_id
            else "Customer portal account-credit direct-transfer intent"
        ),
        idempotency_key=(str(idempotency_key or "").strip() or None),
    )
    db_session_adapter.release_read_transaction(db)
    result = direct_transfer_intents.create_direct_transfer_intent(
        db,
        command,
        context=context,
    )
    return {
        **result.to_dict(),
        "redirect_url": "/portal/billing/topup/transfer",
    }


def get_direct_transfer_topup_page(db: Session, customer: dict) -> dict:
    """Build context for the customer direct-transfer instruction page."""
    if not direct_bank_transfer_enabled(db):
        raise ValueError("Direct bank transfer is not configured")
    account_id = _customer_account_uuid(db, customer)
    intent = _latest_pending_direct_transfer_intent(db, account_id)
    if not intent:
        raise ValueError("Start a direct bank transfer payment first")
    return {
        "intent": intent,
        "bank_transfer": direct_bank_transfer_settings(db),
        "bank_transfer_accounts": enabled_direct_bank_transfer_accounts(db),
        "withholding_tax": dict((intent.metadata_ or {}).get("withholding_tax") or {}),
    }


async def submit_direct_transfer_topup(
    db: Session,
    customer: dict,
    *,
    made_payment: bool,
    file: UploadFile,
    selected_account_id: str | None = None,
) -> dict:
    """Submit the pending direct-transfer top-up for admin review."""
    if not made_payment:
        raise ValueError("Confirm that you have made the payment")
    if not direct_bank_transfer_enabled(db):
        raise ValueError("Direct bank transfer is not configured")

    account_id = _customer_account_uuid(db, customer)
    intent = _latest_pending_direct_transfer_intent(db, account_id)
    if not intent:
        raise ValueError("Start a direct bank transfer payment first")
    accounts = enabled_direct_bank_transfer_accounts(db)
    if not accounts:
        raise ValueError("Direct bank transfer is not configured")
    selected_account = accounts[0]
    if len(accounts) > 1:
        selected_account_id = str(selected_account_id or "").strip()
        selected_account = next(
            (
                account
                for account in accounts
                if str(account.get("id")) == selected_account_id
            ),
            None,
        )
        if not selected_account:
            raise ValueError("Choose the bank account you paid into")

    from app.services import payment_proofs

    path = await payment_proofs.save_proof_file(file)
    submitted_by = uuid.UUID(
        str(optional_customer_subscriber_id(db, customer) or account_id)
    )
    command = payment_proofs.DirectTransferProofSubmissionCommand(
        intent_id=intent.id,
        account_id=account_id,
        submitted_by=submitted_by,
        selected_bank_account=DirectTransferBankAccountEvidence(
            id=str(selected_account.get("id") or ""),
            bank_name=str(selected_account.get("bank_name") or ""),
            account_name=str(selected_account.get("account_name") or ""),
            account_number=str(selected_account.get("account_number") or ""),
            sort_code=str(selected_account.get("sort_code") or ""),
        ),
        paid_at=datetime.now(UTC),
        file_path=path,
    )
    db_session_adapter.release_read_transaction(db)
    proof_result = payment_proofs.submit_direct_transfer_proof(
        db,
        command,
        context=CommandContext.system(
            actor=f"customer:{command.submitted_by}",
            scope=payment_proofs.SUBMISSION_SCOPE,
            reason="Customer portal direct-transfer evidence submission",
        ),
    )
    return proof_result.to_dict()


def verify_and_record_topup(
    db: Session,
    customer: dict,
    reference: str,
    *,
    provider: str | None = None,
) -> dict:
    """Verify and atomically settle a Deposit Account Credit receipt."""
    account_id = _customer_account_uuid(db, customer)
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()
    if not intent:
        raise ValueError("Payment reference was not issued for this add-funds flow")
    if intent.account_id != account_id:
        raise ValueError("Payment reference does not belong to this account")
    provider_type = provider_for_intent(intent, provider).value
    if intent.purpose is None:
        return _verify_and_record_legacy_topup(
            db,
            intent=intent,
            reference=reference,
            provider_type=provider_type,
        )
    tx = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
        capability_binding_id=(
            str(intent.capability_binding_id) if intent.capability_binding_id else None
        ),
    )
    metadata = dict(tx.metadata or {})
    metadata_intent_id = str(metadata.get("topup_intent_id") or "")
    try:
        provider_intent_id = uuid.UUID(metadata_intent_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Verified payment did not identify the original checkout session"
        ) from exc
    if provider_intent_id != intent.id:
        raise ValueError("Verified payment did not match the original checkout session")

    command = SettleAccountCreditDepositCommand(
        intent_id=intent.id,
        provider_type=tx.provider_type,
        external_transaction_id=tx.external_id,
        amount=tx.amount,
        currency=tx.currency,
        provider_intent_id=provider_intent_id,
        source=AccountCreditDepositSettlementSource.customer_gateway_verify,
        provider_fee=round_money(to_decimal(getattr(tx, "provider_fee", 0))),
    )
    context = CommandContext.system(
        actor=f"customer:{account_id}",
        scope=SETTLEMENT_SCOPE,
        reason="Settle customer-verified account-credit deposit",
        idempotency_key=f"account-credit-deposit-{intent.id}",
    )
    db_session_adapter.release_read_transaction(db)
    settlement = AccountCreditDeposits.settle_verified(
        db,
        command,
        context=context,
    )
    payment = db.get(Payment, settlement.payment_id)
    settled_intent = db.get(TopupIntent, settlement.intent_id)
    if payment is None or settled_intent is None:
        raise ValueError("Deposit settlement evidence is unavailable")
    return _build_topup_result(
        db,
        payment=payment,
        intent=settled_intent,
        amount=settlement.amount,
        reference=reference,
        already_recorded=settlement.already_recorded,
    )


__all__ = [
    "complete_invoice_payment_intent",
    "create_invoice_payment_intent",
    "create_topup_intent",
    "create_direct_transfer_topup_intent",
    "direct_bank_transfer_enabled",
    "direct_bank_transfer_settings",
    "enabled_direct_bank_transfer_accounts",
    "get_direct_transfer_topup_page",
    "get_payment_page",
    "get_topup_page",
    "preview_topup",
    "submit_direct_transfer_topup",
    "verify_and_record_payment",
    "verify_and_record_topup",
]
