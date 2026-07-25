"""Safe condition evaluation for automated notification templates."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.customer_support_links import ticket_customer_link_filter


class NotificationTemplateConditionError(ValueError):
    """Raised when notification template conditions are malformed."""


CONDITION_FIELD_HELP: tuple[tuple[str, str, str], ...] = (
    (
        "customer_has_open_ticket",
        "true",
        "Customer has at least one active support ticket.",
    ),
    (
        "open_ticket_count",
        "1",
        "Number of active support tickets linked to the customer.",
    ),
    ("account_status", "active", "Customer account status."),
    (
        "has_overdue_invoice",
        "true",
        "Customer has at least one overdue invoice with a balance due.",
    ),
    (
        "overdue_invoice_count",
        "1",
        "Number of overdue invoices with a balance due.",
    ),
    (
        "has_active_dunning_case",
        "false",
        "Customer has an open or paused dunning case.",
    ),
    (
        "active_subscription_count",
        "1",
        "Number of active subscriptions for the customer.",
    ),
    (
        "has_active_subscription",
        "true",
        "Customer has at least one active subscription.",
    ),
)

_ALLOWED_FIELDS = {field for field, _sample, _description in CONDITION_FIELD_HELP}
_BOOLEAN_FIELDS = {
    "customer_has_open_ticket",
    "has_overdue_invoice",
    "has_active_dunning_case",
    "has_active_subscription",
}
_NUMBER_FIELDS = {
    "open_ticket_count",
    "overdue_invoice_count",
    "active_subscription_count",
}
_TEXT_FIELDS = {"account_status"}
_OPEN_TICKET_STATUSES = {
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
}
_DUNNING_ACTIVE_STATUSES = {"open", "paused"}


def normalize_conditions(conditions: Any) -> dict[str, list[dict[str, Any]]]:
    """Validate and normalize condition JSON.

    Accepted shapes:
    - ``{}`` or ``None``: no conditions
    - ``{"field": "...", "operator": "=", "value": ...}``: one condition
    - ``[{"field": "...", "operator": "=", "value": ...}]``: all conditions
    - ``{"all": [...], "any": [...]}``: grouped conditions
    """

    if conditions in (None, "", {}):
        return {}
    if isinstance(conditions, list):
        return _normalize_groups({"all": conditions})
    if not isinstance(conditions, dict):
        raise NotificationTemplateConditionError("Conditions must be a JSON object")
    if "field" in conditions:
        return _normalize_groups({"all": [conditions]})
    return _normalize_groups(conditions)


def validate_conditions(conditions: Any) -> dict[str, list[dict[str, Any]]]:
    """Return normalized conditions or raise a user-facing validation error."""

    return normalize_conditions(conditions)


def conditions_match(
    db: Session,
    *,
    subscriber_id: UUID | None,
    conditions: Any,
) -> bool:
    normalized = normalize_conditions(conditions)
    if not normalized:
        return True

    evaluator = _ConditionEvaluator(db, subscriber_id=subscriber_id)
    for condition in normalized.get("all", []):
        if not evaluator.evaluate(condition):
            return False
    any_conditions = normalized.get("any", [])
    if any_conditions and not any(evaluator.evaluate(item) for item in any_conditions):
        return False
    return True


def conditions_match_for_subscribers(
    db: Session,
    *,
    subscriber_ids: tuple[UUID, ...],
    conditions: Any,
) -> dict[UUID, bool]:
    """Evaluate one normalized condition set for a cohort in bounded queries."""

    normalized = normalize_conditions(conditions)
    unique_ids = tuple(dict.fromkeys(subscriber_ids))
    if not unique_ids:
        return {}
    if not normalized:
        return dict.fromkeys(unique_ids, True)

    fields = {
        str(condition["field"]) for group in normalized.values() for condition in group
    }
    facts: dict[UUID, dict[str, object]] = {
        subscriber_id: {} for subscriber_id in unique_ids
    }

    if fields & {"customer_has_open_ticket", "open_ticket_count"}:
        from app.models.support import Ticket

        counts = dict.fromkeys(unique_ids, 0)
        rows = db.execute(
            select(
                Ticket.subscriber_id,
                Ticket.customer_account_id,
                Ticket.customer_person_id,
            )
            .where(Ticket.is_active.is_(True))
            .where(Ticket.status.in_(_OPEN_TICKET_STATUSES))
            .where(
                Ticket.subscriber_id.in_(unique_ids)
                | Ticket.customer_account_id.in_(unique_ids)
                | Ticket.customer_person_id.in_(unique_ids)
            )
        ).all()
        for row in rows:
            for linked_id in set(row):
                if linked_id in counts:
                    counts[linked_id] += 1
        for subscriber_id, count in counts.items():
            facts[subscriber_id]["open_ticket_count"] = count
            facts[subscriber_id]["customer_has_open_ticket"] = count > 0

    if "account_status" in fields:
        from app.models.subscriber import Subscriber

        statuses: dict[UUID, object] = {
            subscriber_id: status
            for subscriber_id, status in db.execute(
                select(Subscriber.id, Subscriber.status).where(
                    Subscriber.id.in_(unique_ids)
                )
            ).tuples()
        }
        for subscriber_id in unique_ids:
            status = statuses.get(subscriber_id)
            facts[subscriber_id]["account_status"] = str(
                getattr(status, "value", status) or ""
            )

    if fields & {"has_overdue_invoice", "overdue_invoice_count"}:
        from app.models.billing import Invoice
        from app.services.invoice_collectibility import overdue_status_filters

        invoice_counts: dict[UUID, int] = {
            account_id: int(count)
            for account_id, count in db.execute(
                select(Invoice.account_id, func.count(Invoice.id))
                .where(Invoice.account_id.in_(unique_ids))
                .where(*overdue_status_filters())
                .group_by(Invoice.account_id)
            ).tuples()
            if account_id is not None
        }
        for subscriber_id in unique_ids:
            count = int(invoice_counts.get(subscriber_id, 0))
            facts[subscriber_id]["overdue_invoice_count"] = count
            facts[subscriber_id]["has_overdue_invoice"] = count > 0

    if "has_active_dunning_case" in fields:
        from app.models.collections import DunningCase

        dunning_counts: dict[UUID, int] = {
            account_id: int(count)
            for account_id, count in db.execute(
                select(DunningCase.account_id, func.count(DunningCase.id))
                .where(DunningCase.account_id.in_(unique_ids))
                .where(DunningCase.status.in_(_DUNNING_ACTIVE_STATUSES))
                .group_by(DunningCase.account_id)
            ).tuples()
            if account_id is not None
        }
        for subscriber_id in unique_ids:
            facts[subscriber_id]["has_active_dunning_case"] = (
                int(dunning_counts.get(subscriber_id, 0)) > 0
            )

    if fields & {"active_subscription_count", "has_active_subscription"}:
        from app.models.catalog import Subscription, SubscriptionStatus

        subscription_counts: dict[UUID, int] = {
            subscriber_id: int(count)
            for subscriber_id, count in db.execute(
                select(Subscription.subscriber_id, func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(unique_ids))
                .where(Subscription.status == SubscriptionStatus.active)
                .group_by(Subscription.subscriber_id)
            ).tuples()
            if subscriber_id is not None
        }
        for subscriber_id in unique_ids:
            count = int(subscription_counts.get(subscriber_id, 0))
            facts[subscriber_id]["active_subscription_count"] = count
            facts[subscriber_id]["has_active_subscription"] = count > 0

    return {
        subscriber_id: _conditions_match_facts(
            facts[subscriber_id],
            normalized,
        )
        for subscriber_id in unique_ids
    }


def _conditions_match_facts(
    facts: dict[str, object],
    normalized: dict[str, list[dict[str, Any]]],
) -> bool:
    def _matches(condition: dict[str, Any]) -> bool:
        field = str(condition["field"])
        expected = _coerce_expected(
            field,
            condition.get("value"),
            str(condition["operator"]),
        )
        return _compare(facts.get(field), str(condition["operator"]), expected)

    if any(not _matches(condition) for condition in normalized.get("all", [])):
        return False
    any_conditions = normalized.get("any", [])
    return not any_conditions or any(
        _matches(condition) for condition in any_conditions
    )


def _normalize_groups(conditions: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    allowed_keys = {"all", "any"}
    unknown_keys = set(conditions) - allowed_keys
    if unknown_keys:
        raise NotificationTemplateConditionError(
            "Unknown condition group(s): " + ", ".join(sorted(unknown_keys))
        )

    normalized: dict[str, list[dict[str, Any]]] = {}
    for group_name in ("all", "any"):
        raw_group = conditions.get(group_name, [])
        if raw_group in (None, ""):
            raw_group = []
        if not isinstance(raw_group, list):
            raise NotificationTemplateConditionError(
                f"Condition group '{group_name}' must be an array"
            )
        group = [_normalize_condition(item) for item in raw_group]
        if group:
            normalized[group_name] = group
    return normalized


def _normalize_condition(condition: Any) -> dict[str, Any]:
    if not isinstance(condition, dict):
        raise NotificationTemplateConditionError("Each condition must be an object")

    field = str(condition.get("field") or "").strip()
    if field not in _ALLOWED_FIELDS:
        raise NotificationTemplateConditionError(
            f"Unsupported condition field: {field or '(missing)'}"
        )
    operator = _normalize_operator(condition.get("operator") or "=")
    value = condition.get("value")
    _validate_operator_for_field(field, operator)
    _coerce_expected(field, value, operator)
    return {"field": field, "operator": operator, "value": value}


def _normalize_operator(operator: Any) -> str:
    op = str(operator or "").strip().lower()
    aliases = {"==": "=", "eq": "=", "ne": "!=", "not_equals": "!="}
    return aliases.get(op, op)


def _validate_operator_for_field(field: str, operator: str) -> None:
    if field in _BOOLEAN_FIELDS and operator not in {"=", "!="}:
        raise NotificationTemplateConditionError(
            f"Operator '{operator}' is not allowed for boolean field '{field}'"
        )
    if field in _TEXT_FIELDS and operator not in {"=", "!=", "in", "not in"}:
        raise NotificationTemplateConditionError(
            f"Operator '{operator}' is not allowed for text field '{field}'"
        )
    if field in _NUMBER_FIELDS and operator not in {"=", "!=", ">", ">=", "<", "<="}:
        raise NotificationTemplateConditionError(
            f"Operator '{operator}' is not allowed for number field '{field}'"
        )


def _coerce_expected(field: str, value: Any, operator: str = "=") -> object:
    if field in _BOOLEAN_FIELDS:
        return _coerce_bool(value)
    if field in _NUMBER_FIELDS:
        return _coerce_decimal(value)
    if field in _TEXT_FIELDS:
        if operator in {"in", "not in"}:
            if not isinstance(value, list) or not value:
                raise NotificationTemplateConditionError(
                    f"Operator '{operator}' for field '{field}' requires a non-empty array"
                )
            return [str(item).strip().lower() for item in value]
        if value is None:
            raise NotificationTemplateConditionError(
                f"Field '{field}' requires a value"
            )
        return str(value).strip().lower()
    raise NotificationTemplateConditionError(f"Unsupported condition field: {field}")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise NotificationTemplateConditionError("Expected a boolean condition value")


def _coerce_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise NotificationTemplateConditionError(
            "Expected a numeric condition value"
        ) from exc


class _ConditionEvaluator:
    def __init__(self, db: Session, *, subscriber_id: UUID | None) -> None:
        self.db = db
        self.subscriber_id = subscriber_id
        self._cache: dict[str, object] = {}

    def evaluate(self, condition: dict[str, Any]) -> bool:
        field = condition["field"]
        operator = condition["operator"]
        expected = _coerce_expected(field, condition.get("value"), operator)
        actual = self._value(field)
        return _compare(actual, operator, expected)

    def _value(self, field: str) -> object:
        if field not in self._cache:
            self._cache[field] = self._load_value(field)
        return self._cache[field]

    def _load_value(self, field: str) -> object:
        if field == "customer_has_open_ticket":
            return self._open_ticket_count() > 0
        if field == "open_ticket_count":
            return self._open_ticket_count()
        if field == "account_status":
            return self._account_status()
        if field == "has_overdue_invoice":
            return self._overdue_invoice_count() > 0
        if field == "overdue_invoice_count":
            return self._overdue_invoice_count()
        if field == "has_active_dunning_case":
            return self._active_dunning_case_count() > 0
        if field == "active_subscription_count":
            return self._active_subscription_count()
        if field == "has_active_subscription":
            return self._active_subscription_count() > 0
        raise NotificationTemplateConditionError(
            f"Unsupported condition field: {field}"
        )

    def _open_ticket_count(self) -> int:
        if self.subscriber_id is None:
            return 0
        from app.models.support import Ticket

        return (
            self.db.query(Ticket.id)
            .filter(Ticket.is_active.is_(True))
            .filter(Ticket.status.in_(_OPEN_TICKET_STATUSES))
            .filter(ticket_customer_link_filter(Ticket, self.subscriber_id))
            .count()
        )

    def _account_status(self) -> str | None:
        if self.subscriber_id is None:
            return None
        from app.models.subscriber import Subscriber

        subscriber = self.db.get(Subscriber, self.subscriber_id)
        if not subscriber:
            return None
        status = subscriber.status
        return status.value if hasattr(status, "value") else str(status)

    def _overdue_invoice_count(self) -> int:
        if self.subscriber_id is None:
            return 0
        from app.services.invoice_collectibility import overdue_status_count

        return overdue_status_count(self.db, self.subscriber_id)

    def _active_dunning_case_count(self) -> int:
        if self.subscriber_id is None:
            return 0
        from app.models.collections import DunningCase

        return (
            self.db.query(DunningCase.id)
            .filter(DunningCase.account_id == self.subscriber_id)
            .filter(DunningCase.status.in_(_DUNNING_ACTIVE_STATUSES))
            .count()
        )

    def _active_subscription_count(self) -> int:
        if self.subscriber_id is None:
            return 0
        from app.models.catalog import Subscription, SubscriptionStatus

        return (
            self.db.query(Subscription.id)
            .filter(Subscription.subscriber_id == self.subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.active)
            .count()
        )


def _compare(actual: object, operator: str, expected: object) -> bool:
    if operator in {"=", "!="}:
        matched = _normalized(actual) == _normalized(expected)
        return matched if operator == "=" else not matched
    if operator in {"in", "not in"}:
        if not isinstance(expected, list):
            raise NotificationTemplateConditionError(
                f"Operator '{operator}' expects an array value"
            )
        actual_value = _normalized(actual)
        expected_values = {_normalized(item) for item in expected}
        matched = actual_value in expected_values
        return matched if operator == "in" else not matched

    actual_number = _coerce_decimal(actual)
    expected_number = _coerce_decimal(expected)
    if operator == ">":
        return actual_number > expected_number
    if operator == ">=":
        return actual_number >= expected_number
    if operator == "<":
        return actual_number < expected_number
    if operator == "<=":
        return actual_number <= expected_number
    raise NotificationTemplateConditionError(
        f"Unsupported condition operator: {operator}"
    )


def _normalized(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return value
    if value is None:
        return None
    return str(value).strip().lower()
