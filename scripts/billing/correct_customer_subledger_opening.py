"""Preview or apply one reviewed customer-subledger opening correction."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from uuid import UUID

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.billing.subledger_opening import (
    CORRECTION_SCOPE,
    CorrectCustomerSubledgerOpeningCommand,
    PreviewCustomerSubledgerOpeningCorrectionQuery,
    correct_customer_subledger_opening_position,
    preview_customer_subledger_opening_correction,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.system_user_assignments import system_user_role_names


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--currency", default="NGN")
    parser.add_argument("--corrected-opening-amount", type=Decimal, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--review-reference", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-preview-fingerprint")
    parser.add_argument("--command-id", type=UUID)
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--idempotency-key")
    return parser


def _preview_dict(value) -> dict[str, object]:  # noqa: ANN001
    return {
        "opening_position_id": str(value.opening_position_id),
        "account_id": str(value.account_id),
        "currency": value.currency,
        "previous_opening_amount": str(value.previous_opening_amount),
        "corrected_opening_amount": str(value.corrected_opening_amount),
        "delta": str(value.delta),
        "preview_fingerprint": value.preview_fingerprint,
    }


def _permission_granted(db, actor_system_user_id: UUID | None) -> bool:  # noqa: ANN001
    if actor_system_user_id is None:
        return False
    user = db.get(SystemUser, actor_system_user_id)
    if user is None or not user.is_active:
        return False
    return has_permission(
        {
            "principal_id": str(actor_system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, actor_system_user_id)),
        },
        db,
        CORRECTION_SCOPE,
    )


def main() -> int:
    args = _parser().parse_args()
    query = PreviewCustomerSubledgerOpeningCorrectionQuery(
        account_id=args.account_id,
        currency=args.currency,
        corrected_opening_amount=args.corrected_opening_amount,
        reason=args.reason,
        review_reference=args.review_reference,
    )
    try:
        with db_session_adapter.read_session() as db:
            preview = preview_customer_subledger_opening_correction(db, query)
            permission_granted = _permission_granted(db, args.actor_system_user_id)
        if not args.apply:
            print(
                json.dumps(
                    {"applied": False, "preview": _preview_dict(preview)},
                    sort_keys=True,
                )
            )
            return 0
        if not all(
            (
                args.expected_preview_fingerprint,
                args.command_id,
                args.actor,
                args.actor_system_user_id,
                args.idempotency_key,
            )
        ):
            print(
                json.dumps(
                    {
                        "applied": False,
                        "error": "--apply requires fingerprint, command ID, actor, staff ID, and idempotency key",
                        "preview": _preview_dict(preview),
                    },
                    sort_keys=True,
                )
            )
            return 2
        with db_session_adapter.owner_command_session() as db:
            outcome = correct_customer_subledger_opening_position(
                db,
                CorrectCustomerSubledgerOpeningCommand(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope=CORRECTION_SCOPE,
                        reason=args.reason,
                        command_id=args.command_id,
                        correlation_id=args.command_id,
                        idempotency_key=args.idempotency_key,
                    ),
                    query=query,
                    expected_preview_fingerprint=args.expected_preview_fingerprint,
                    permission_granted=permission_granted,
                    authorized_system_user_id=args.actor_system_user_id,
                ),
            )
    except DomainError as exc:
        print(
            json.dumps(
                {
                    "applied": False,
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "applied": True,
                "correction_id": str(outcome.correction_id),
                "posting_group_id": str(outcome.posting_group_id),
                "previous_opening_amount": str(outcome.previous_opening_amount),
                "corrected_opening_amount": str(outcome.corrected_opening_amount),
                "delta": str(outcome.delta),
                "replayed": outcome.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
