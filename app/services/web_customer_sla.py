"""Typed web projection for the admin-only SLA shadow review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.schemas.status_presentation import StatusPresentation
from app.services.customer_service_level import review_admin_period
from app.services.domain_errors import DomainError
from app.services.service_impact_contracts import SLA_CALENDAR_TIMEZONE
from app.services.sla_admin_review import SlaAdminReview, SlaAdminReviewQuery

_PERIOD_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")


class SlaAdminReviewPageError(DomainError):
    """The admin page request could not be mapped to a review query."""


@dataclass(frozen=True, slots=True)
class SlaAdminPeriodOption:
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class SlaAdminReviewPage:
    """One restricted admin page; templates do not derive domain meaning."""

    review: SlaAdminReview
    selected_period: str
    period_label: str
    periods: tuple[SlaAdminPeriodOption, ...]
    candidate_presentation: StatusPresentation | None
    customer_url: str
    legacy_url: str


def humanize_seconds(seconds: int) -> str:
    """Compact duration formatting at the presentation boundary."""

    if seconds <= 0:
        return "none"
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _month_before(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _period_bounds(period: str, *, evaluated_at: datetime) -> tuple[datetime, datetime]:
    match = _PERIOD_PATTERN.fullmatch(period)
    if match is None:
        raise SlaAdminReviewPageError(
            code="customer.service_level.invalid_review_period",
            message="SLA review period must use YYYY-MM.",
            details={"period": period},
        )
    year = int(match.group("year"))
    month = int(match.group("month"))
    zone = ZoneInfo(SLA_CALENDAR_TIMEZONE)
    start = datetime(year, month, 1, tzinfo=zone)
    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    end = datetime(next_year, next_month, 1, tzinfo=zone)
    if end > evaluated_at.astimezone(zone):
        raise SlaAdminReviewPageError(
            code="customer.service_level.review_period_not_closed",
            message="Choose a closed calendar month for SLA review.",
            details={"period": period},
        )
    return start.astimezone(UTC), end.astimezone(UTC)


def _period_options(*, evaluated_at: datetime) -> tuple[SlaAdminPeriodOption, ...]:
    local = evaluated_at.astimezone(ZoneInfo(SLA_CALENDAR_TIMEZONE))
    year, month = _month_before(local.year, local.month)
    options: list[SlaAdminPeriodOption] = []
    for _index in range(12):
        start = datetime(year, month, 1)
        options.append(
            SlaAdminPeriodOption(
                key=f"{year:04d}-{month:02d}",
                label=start.strftime("%B %Y"),
            )
        )
        year, month = _month_before(year, month)
    return tuple(options)


def build_sla_admin_review_page(
    db: Session,
    *,
    subscriber_id: UUID,
    subscription_id: UUID,
    period: str | None,
    now: datetime | None = None,
) -> SlaAdminReviewPage:
    """Build one exact-period comparison from the customer.service_level owner."""

    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    options = _period_options(evaluated_at=evaluated_at)
    selected = period or options[0].key
    start, end = _period_bounds(selected, evaluated_at=evaluated_at)
    review = review_admin_period(
        db,
        SlaAdminReviewQuery(
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            period_start=start,
            period_end=end,
            evaluated_at=evaluated_at,
        ),
    )
    selected_option = next((item for item in options if item.key == selected), None)
    if selected_option is not None:
        label = selected_option.label
    else:
        match = _PERIOD_PATTERN.fullmatch(selected)
        assert match is not None  # validated by _period_bounds
        label = datetime(
            int(match.group("year")), int(match.group("month")), 1
        ).strftime("%B %Y")
    candidate_presentation: StatusPresentation | None = None
    if review.candidate is not None:
        from app.services.status_presentation import sla_verdict_presentation

        candidate_presentation = sla_verdict_presentation(review.candidate.verdict)
    return SlaAdminReviewPage(
        review=review,
        selected_period=selected,
        period_label=label,
        periods=options,
        candidate_presentation=candidate_presentation,
        customer_url=f"/admin/customers/{subscriber_id}",
        legacy_url=f"/admin/customers/{subscriber_id}/availability",
    )


__all__ = (
    "SlaAdminPeriodOption",
    "SlaAdminReviewPage",
    "SlaAdminReviewPageError",
    "build_sla_admin_review_page",
    "humanize_seconds",
)
