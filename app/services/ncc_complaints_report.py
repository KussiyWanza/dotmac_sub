"""NCC Quarterly Complaints return (①) — built from sub's native tickets.

Re-homed from CRM for the CRM exit. The record shape, cleaners and reference
vocabulary are shared with :mod:`app.services.ncc_workbook`, so the JSON the
pack renders and the XLSX the officer files always agree.

Four deliberate divergences from CRM's implementation, each removing a value
we could not honestly source:

1. **Category/sub-category are read, not guessed.** CRM keyword-matched free
   text at report time. Here the classification is derived on save and stored
   on the ticket (:mod:`app.services.ncc_categorisation`), so an agent can
   correct it and the correction is what gets filed. A ticket with no stored
   category reports blank — visible as ``[FAIL]`` at export — rather than
   being silently re-guessed.
2. **Status: Closed tickets file as Resolved.** ``Resolved`` is the NCC return's
   external vocabulary, not a native Ticket status. ``canceled``/``merged`` are
   excluded entirely: they are not complaints.
3. **SLA: unknown is blank, not a breach.** CRM returned ``"No"`` when
   ``due_at`` was NULL, reporting an SLA breach that may never have happened.
   A missing due date means we do not know, and the return says so.
4. **Location is never invented.** CRM tried nine address sources and defaulted
   anything unmatched to "Municipal Area Council, FEDERAL CAPITAL TERRITORY" —
   an unlocatable complainant became an Abuja statistic. Here State/LGA/Town
   come from what we actually hold, canonicalised against
   :mod:`app.services.ncc_location`'s reference tables; unresolvable stays
   blank.
5. **Internal operations are not customer complaints.** Tickets carrying an
   approved internal operational source are excluded. Other tickets remain
   visible even when customer identity, classification, SLA, or location facts
   are incomplete, and the workbook reports those gaps as ``[FAIL]``.

Known gaps, reported blank rather than fabricated: sub has no ``Person``
model, so **alt phone** has no source at all (subscribers carry one phone),
and **Age/Gender** exist only for subscriber-linked complaints. Customer phone
identity is not rewritten; the report alone projects supported Nigerian phone
forms into NCC's ``234XXXXXXXXXX`` filing format.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketComment
from app.services import ncc_location
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.domain_errors import DomainError
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.ncc_subscriber_report import _UNKNOWN, infer_state, normalize_state
from app.services.ncc_workbook import (
    COLUMNS,
    category_code_value,
    clean_basic_text,
    clean_category,
    clean_subcategory_code,
    clean_text,
    name_contains_test,
    validation_status,
)
from app.services.ncc_workbook import (
    SUBCATEGORY_BY_CODE as _SUBCATEGORY_BY_CODE,
)
from app.services.support_ticket_contracts import InternalOperationalTicketSource

OPERATOR_PREFIX = "DOTMAC"

NCC_COMPLAINTS_LIST_DEFINITION = ListDefinition(
    key="ncc-complaints",
    fields=(
        ListFieldDefinition(key="created_at", label="Created", sortable=True),
        ListFieldDefinition(key="date_from", label="From", filterable=True),
        ListFieldDefinition(key="date_to", label="To", filterable=True),
    ),
    default_sort="created_at",
    default_sort_dir="asc",
    default_per_page=20,
    per_page_options=(20, 50, 100),
)


class NccComplaintsReportError(DomainError):
    """Transport-neutral NCC report query error."""


class NccComplaintAudience(str, Enum):
    """Typed filing-scope decision for one authoritative Ticket."""

    customer_complaint = "customer_complaint"
    internal_operational = "internal_operational"


@dataclass(frozen=True, slots=True)
class NccMsisdnProjection:
    """NCC-only phone projection without changing stored customer identity."""

    value: str


@dataclass(frozen=True, slots=True)
class NccComplaintsReportQuery:
    """Exact inclusive UTC complaint-created window requested by a caller."""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class NccComplaintRecord:
    """One row whose positional values are locked to the NCC column contract."""

    values: tuple[str, ...]
    status_variant: str

    @classmethod
    def from_mapping(cls, record: dict[str, str]) -> NccComplaintRecord:
        return cls(
            values=tuple(str(record.get(column, "")) for column in COLUMNS),
            status_variant=str(record.get("_status_variant", "")),
        )

    def as_mapping(self) -> dict[str, str]:
        result = dict(zip(COLUMNS, self.values, strict=True))
        result["_status_variant"] = self.status_variant
        return result


@dataclass(frozen=True, slots=True)
class NccComplaintsReportSnapshot:
    """Typed, deterministic report projection consumed by UI and delivery."""

    query: NccComplaintsReportQuery
    records: tuple[NccComplaintRecord, ...]
    by_category: tuple[tuple[str, int], ...]
    by_status: tuple[tuple[str, int], ...]
    unclassified_count: int

    @property
    def total_complaints(self) -> int:
        return len(self.records)

    def record_mappings(self) -> list[dict[str, str]]:
        return [record.as_mapping() for record in self.records]

    def as_legacy_dict(self) -> dict[str, Any]:
        return {
            "total_complaints": self.total_complaints,
            "by_category": dict(self.by_category),
            "by_status": dict(self.by_status),
            "unclassified_count": self.unclassified_count,
            "columns": list(COLUMNS),
            "records": self.record_mappings(),
        }


@dataclass(frozen=True, slots=True)
class NccComplaintsTablePage:
    """Typed on-screen page over a complete NCC report snapshot."""

    list_query: ListQuery
    page_meta: PageMeta
    records: tuple[NccComplaintRecord, ...]


def paginate_report(
    snapshot: NccComplaintsReportSnapshot,
    *,
    list_query: ListQuery,
) -> NccComplaintsTablePage:
    """Page only the UI projection; filing and delivery retain every row."""

    page_meta = PageMeta.from_query(list_query, snapshot.total_complaints)
    effective_query = list_query.with_page(page_meta.page)
    start = effective_query.offset
    return NccComplaintsTablePage(
        list_query=effective_query,
        page_meta=page_meta,
        records=snapshot.records[start : start + effective_query.per_page],
    )


# NCC files complaints as Resolved or Pending. Michael's call (2026-07-17):
# Closed is the one native status for a resolved complaint. The filing label
# remains "Resolved" because that is the NCC workbook's external vocabulary.
_RESOLVED_STATUSES = frozenset({"closed"})
# Not complaints: a canceled ticket was withdrawn, a merged one is counted
# under the ticket it merged into. Filing either would double-count or invent.
_EXCLUDED_STATUSES = frozenset({"canceled", "merged"})

_TICKET_SOURCE_BY_CHANNEL = {
    "phone": "Phone Call",
    "email": "Email",
    "web": "Web Portal",
    "chat": "Web Portal",
    "api": "Other",
}

_GENERIC_NOTE_MARKERS = (
    "kindly treat",
    "whats the update",
    "what's the update",
    "please treat",
    "assigned",
    "escalated",
    "resolution sent to the customer for confirmation",
)
_NOTE_LEADING_GROUP_MENTION_RE = re.compile(
    r"^@\s*[^()\n\r]{1,120}\([^)]*\)\s*", re.IGNORECASE
)
_NOTE_LEADING_ROUTING_MENTION_RE = re.compile(
    r"^@\s*[^@\n\r,;:.]{1,80}\s*[,;:.+-]\s*", re.IGNORECASE
)
_NOTE_GROUP_MENTION_RE = re.compile(r"@\s*[^@,;:.\n\r]{1,120}\([^)]*\)", re.IGNORECASE)
_NOTE_PERSON_MENTION_RE = re.compile(
    r"@\s*[A-Za-z][A-Za-z0-9.'_-]*(?:\s+[A-Z][A-Za-z.'_-]*)?"
)

_PHONE_TYPE_METADATA_KEYS = (
    "phone_type",
    "phone type",
    "device_make_model",
    "device make model",
    "device_model",
    "device model",
    "phone_model",
    "phone model",
    "device",
)
_RESOLUTION_NOTE_METADATA_KEYS = (
    "resolution_note",
    "resolution_notes",
    "resolution_details",
    "resolution_summary",
    "closure_note",
)


# ── small shared helpers ─────────────────────────────────────────────────────
def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _display_timestamp(value: datetime | None) -> str:
    normalized = _as_utc(value)
    if normalized is None or normalized > datetime.now(UTC):
        return ""
    return normalized.strftime("%d-%m-%Y %H:%M:%S")


def _clean_long_text(value: object) -> str:
    cleaned = clean_basic_text(value)
    if not cleaned:
        return ""
    if cleaned.isupper():
        cleaned = cleaned.lower()
    return cleaned[:1].upper() + cleaned[1:]


def _status_value(ticket: Ticket) -> str:
    status = str(getattr(ticket.status, "value", ticket.status) or "").strip().lower()
    return "Resolved" if status in _RESOLVED_STATUSES else "Pending"


def _complaint_audience(ticket: Ticket) -> NccComplaintAudience:
    metadata = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
    opened_by = clean_text(metadata.get("opened_by"))
    if not opened_by:
        return NccComplaintAudience.customer_complaint
    try:
        InternalOperationalTicketSource(opened_by)
    except ValueError:
        return NccComplaintAudience.customer_complaint
    return NccComplaintAudience.internal_operational


def _is_excluded(ticket: Ticket) -> bool:
    status = str(getattr(ticket.status, "value", ticket.status) or "").strip().lower()
    return (
        status in _EXCLUDED_STATUSES
        or _complaint_audience(ticket) is NccComplaintAudience.internal_operational
    )


def _project_ncc_msisdn(value: object) -> NccMsisdnProjection:
    raw = clean_basic_text(value)
    if not raw:
        return NccMsisdnProjection(value="")

    normalized = normalize_phone_identifier(raw)
    projected = normalized.removeprefix("+") if normalized else raw
    return NccMsisdnProjection(value=projected)


def _resolved_within_sla(ticket: Ticket) -> str:
    """Yes/No only when we know. Blank when the SLA target was never set.

    CRM returned "No" for a missing ``due_at`` — filing a breach it had no
    evidence for. An absent due date is an absence of knowledge, not a miss.
    """
    if _status_value(ticket) != "Resolved":
        return ""
    resolved_at = _as_utc(ticket.resolved_at or ticket.closed_at)
    due_at = _as_utc(ticket.due_at)
    if resolved_at is None or due_at is None:
        return ""
    return "Yes" if resolved_at <= due_at else "No"


def _ticket_id(ticket: Ticket) -> str:
    created_at = _as_utc(ticket.created_at) or datetime.now(UTC)
    raw_number = ticket.number or str(ticket.id)
    cleaned = (
        re.sub(r"[^A-Za-z0-9-]+", "", str(raw_number))
        or str(ticket.id).replace("-", "")[:12]
    )
    return f"{OPERATOR_PREFIX}-{created_at:%Y%m%d}-{cleaned}"


def _complaint_type(ticket: Ticket) -> str:
    escalated = bool(
        ticket.service_team_id or ticket.assigned_to_person_id or ticket.assignees
    )
    return "Second Level" if escalated else "First Level"


def _ticket_source(channel: object) -> str:
    value = str(getattr(channel, "value", channel) or "").strip().lower()
    return _TICKET_SOURCE_BY_CHANNEL.get(value, "Other")


def _calculate_age(date_of_birth: date | None, reference_at: datetime | None) -> str:
    if not date_of_birth:
        return "N/A"
    reference = _as_utc(reference_at) or datetime.now(UTC)
    reference_date = reference.date()
    years = reference_date.year - date_of_birth.year
    if (reference_date.month, reference_date.day) < (
        date_of_birth.month,
        date_of_birth.day,
    ):
        years -= 1
    return str(max(years, 0))


def _gender_value(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "N/A"
    gender = str(getattr(subscriber.gender, "value", subscriber.gender) or "").strip()
    if not gender or gender.lower() == "unknown":
        return "N/A"
    return gender.replace("_", " ").title()


def _clean_note_text(value: object) -> str:
    note = clean_text(value)
    note = _NOTE_GROUP_MENTION_RE.sub(" ", note)
    note = _NOTE_PERSON_MENTION_RE.sub(" ", note)
    note = re.sub(r"\s+([,;:.])", r"\1", note)
    note = re.sub(r"(?:\s*[,;]\s*){2,}", ", ", note)
    previous = None
    while note and note != previous:
        previous = note
        note = _NOTE_LEADING_GROUP_MENTION_RE.sub("", note)
        note = _NOTE_LEADING_ROUTING_MENTION_RE.sub("", note).strip(" ,;:-.")
    return _clean_long_text(note.strip(" ,;:-."))


def _meaningful_note(value: object) -> str:
    note = _clean_note_text(value)
    if not note:
        return ""
    lowered = note.lower()
    if any(marker in lowered for marker in _GENERIC_NOTE_MARKERS):
        return ""
    return note


def _ticket_notes(ticket: Ticket) -> tuple[str, str, str]:
    latest_internal: TicketComment | None = None
    latest_meaningful_internal = ""
    latest_meaningful_any = ""

    comments = sorted(
        ticket.comments or [],
        key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC),
    )
    for comment in comments:
        meaningful = _meaningful_note(comment.body)
        if meaningful:
            latest_meaningful_any = meaningful
            if comment.is_internal:
                latest_meaningful_internal = meaningful
        if comment.is_internal:
            latest_internal = comment

    metadata = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
    resolution_note = ""
    for key in _RESOLUTION_NOTE_METADATA_KEYS:
        resolution_note = _meaningful_note(metadata.get(key))
        if resolution_note:
            break
    if not resolution_note:
        resolution_note = latest_meaningful_internal or latest_meaningful_any

    user_note = _clean_note_text(latest_internal.body) if latest_internal else ""
    user_note_dt = (
        _display_timestamp(latest_internal.created_at) if latest_internal else ""
    )
    return resolution_note, user_note, user_note_dt


def _phone_type(ticket: Ticket) -> str:
    metadata = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
    for key in _PHONE_TYPE_METADATA_KEYS:
        value = metadata.get(key)
        if value:
            return clean_text(value)
    return ""


# ── location: from what we hold, never invented ──────────────────────────────
def _captured_lgas(subscriber: Subscriber | None) -> list[str]:
    """Captured LGA values for a subscriber, most authoritative first.

    The subscriber's own field leads: production holds 51 ``addresses`` rows
    against 15,291 subscribers, so the subscriber's own address is the one
    almost every complaint has. Service/primary address rows follow for the
    minority that carry them.
    """
    if subscriber is None:
        return []
    candidates: list[str] = []
    own = str(getattr(subscriber, "lga", "") or "").strip()
    if own:
        candidates.append(own)
    addresses = list(getattr(subscriber, "addresses", None) or [])
    addresses.sort(
        key=lambda a: (
            0 if getattr(a, "is_primary", False) else 1,
            0 if str(getattr(a, "address_type", "")).endswith("service") else 1,
        )
    )
    for address in addresses:
        value = str(getattr(address, "lga", "") or "").strip()
        if value:
            candidates.append(value)
    return candidates


def _ticket_location(
    ticket: Ticket, subscriber: Subscriber | None
) -> tuple[str, str, str]:
    """(state, lga, town) — blank where unknown.

    State comes from two honest sources: the ticket's own ``region`` and the
    subscriber's location (via ``infer_state``, which already spends every
    address/metadata signal we have). Both are canonicalised against the NCC
    reference tables. Nothing is defaulted: CRM turned an unmatched address
    into "Municipal Area Council, FEDERAL CAPITAL TERRITORY", which reported
    subscribers we could not locate as Abuja.

    LGA is read from the CAPTURED ``lga`` — the subscriber's own field first,
    then their service/primary address row — and is validated at capture, so
    it is reported as stored rather than re-interpreted here. It is never
    derived: an uncaptured LGA reports blank, and the workbook's validator
    then tells the compliance officer the row is not filable.

    Town has no captured field, so it stays a canonicalisation of the region
    text against the NCC tables, or blank.
    """
    state = ""
    for candidate in (ticket.region, subscriber):
        if candidate is None:
            continue
        resolved = (
            infer_state(candidate)
            if isinstance(candidate, Subscriber)
            else normalize_state(candidate)
        )
        if resolved and resolved != _UNKNOWN:
            state = resolved
            break

    if not state:
        return "", "", ""

    ncc_state = ncc_location.canonical_state(state)
    if not ncc_state:
        return "", "", ""

    # LGA: the captured field, re-checked against the state we are actually
    # reporting. The check is not redundant with capture-time validation — a
    # later region change can strand a valid-for-the-old-state LGA, and filing
    # "Eti-Osa, Kano" would be a wrong return. A stranded LGA reports blank.
    lga = ""
    for captured in _captured_lgas(subscriber):
        canonical = ncc_location.canonical_lga(ncc_state, captured)
        if canonical:
            lga = canonical
            break

    # Town is only ever a *canonicalisation of what was written* — the region
    # text matched against NCC's own reference tables. Nothing is inferred from
    # proximity or defaulted. An FCT district ("Wuse") maps to its area council
    # deterministically; anything unmatched stays blank.
    town = ""
    if ncc_state == ncc_location.canonical_state("Federal Capital Territory"):
        fct = ncc_location.fct_location_for_town(ticket.region)
        if fct:
            town = fct[1]
            # The FCT table names the area council, which IS the LGA. Only used
            # when nothing was captured — a captured LGA outranks it.
            if not lga:
                lga = ncc_location.canonical_lga(ncc_state, fct[0])
    if not town:
        town = ncc_location.canonical_town(ticket.region)
    return ncc_state, lga, town


def _subscriber_for(
    ticket: Ticket, by_id: dict[uuid.UUID, Subscriber]
) -> Subscriber | None:
    for key in (
        ticket.subscriber_id,
        ticket.customer_person_id,
        ticket.customer_account_id,
    ):
        if key and key in by_id:
            return by_id[key]
    return None


def _record_for(ticket: Ticket, subscriber: Subscriber | None) -> dict[str, str]:
    status = _status_value(ticket)
    # Read the STORED classification. A blank one means nothing captured it —
    # the export's validation flags the row rather than us guessing now.
    category = clean_category(ticket.ncc_category)
    subcategory = clean_subcategory_code(ticket.ncc_subcategory, category=category)
    subcategory_row = _SUBCATEGORY_BY_CODE.get(subcategory.partition(" - ")[0])
    state, lga, town = _ticket_location(ticket, subscriber)
    resolution_note, user_note, user_note_dt = _ticket_notes(ticket)
    msisdn = _project_ncc_msisdn(subscriber.phone if subscriber else "")

    record = {
        "MSISDN": msisdn.value,
        "First Name": clean_basic_text(subscriber.first_name if subscriber else ""),
        "Last Name": clean_basic_text(subscriber.last_name if subscriber else ""),
        "Email": clean_basic_text(subscriber.email if subscriber else ""),
        "Age": _calculate_age(
            subscriber.date_of_birth if subscriber else None, ticket.created_at
        ),
        "Gender": _gender_value(subscriber),
        "created date time": _display_timestamp(ticket.created_at),
        "Subject": _clean_long_text(ticket.title),
        "Category": category,
        "category code (auto)": category_code_value(category),
        "sub category code": subcategory,
        "Description (auto)": _clean_long_text(
            subcategory_row.get("description") if subcategory_row else ""
        ),
        "Ticket ID": _ticket_id(ticket),
        "Complaint type": _complaint_type(ticket),
        "Status": status,
        "Resolved date": _display_timestamp(ticket.resolved_at or ticket.closed_at)
        if status == "Resolved"
        else "",
        "Resolved within SLA": _resolved_within_sla(ticket),
        "Resolution Note": resolution_note,
        "User Note": user_note,
        "user notes datetime": user_note_dt,
        "Language": "English",
        "Ticket source": _ticket_source(ticket.channel),
        # No source in sub: subscribers carry a single phone and there is no
        # Person/PersonChannel model. Blank beats invented.
        "alt phone number": "",
        "created by": "Dotmac",
        "State": state,
        "LGA": lga,
        "Town": town,
        "Phone Type": _phone_type(ticket),
        "_status_variant": status.lower(),
    }
    record["VALIDATION STATUS"] = validation_status(record)
    return record


def _is_test_record(record: dict[str, str]) -> bool:
    if name_contains_test(record["First Name"]) or name_contains_test(
        record["Last Name"]
    ):
        return True
    return clean_text(record["Subject"]).lower() in {"test", "this is a test"}


def build_records(
    db: Session, *, start: datetime, end: datetime
) -> list[dict[str, str]]:
    """The complaint rows for the window, in ``ncc_workbook.COLUMNS`` order.

    Windowed on ``created_at`` (when the complaint reached us), matching CRM.
    """
    tickets = (
        db.execute(
            select(Ticket)
            .options(selectinload(Ticket.comments), selectinload(Ticket.assignees))
            .where(Ticket.created_at >= start, Ticket.created_at <= end)
            .order_by(Ticket.created_at.asc())
        )
        .scalars()
        .unique()
        .all()
    )

    subscriber_ids = {
        key
        for ticket in tickets
        for key in (
            ticket.subscriber_id,
            ticket.customer_person_id,
            ticket.customer_account_id,
        )
        if key
    }
    by_id: dict[uuid.UUID, Subscriber] = {}
    if subscriber_ids:
        by_id = {
            subscriber.id: subscriber
            for subscriber in db.execute(
                select(Subscriber).where(Subscriber.id.in_(subscriber_ids))
            )
            .scalars()
            .all()
        }

    records: list[dict[str, str]] = []
    for ticket in tickets:
        if _is_excluded(ticket):
            continue
        record = _record_for(ticket, _subscriber_for(ticket, by_id))
        if _is_test_record(record):
            continue
        records.append(record)
    return records


def query_report(
    db: Session, query: NccComplaintsReportQuery
) -> NccComplaintsReportSnapshot:
    """Resolve the typed NCC complaints projection without side effects."""

    start = _as_utc(query.start)
    end = _as_utc(query.end)
    if start is None or end is None or start > end:
        raise NccComplaintsReportError(
            code="compliance.ncc_complaints_reporting.invalid_query",
            message="The NCC reporting window is invalid.",
            details={"field": "reporting_window"},
        )
    normalized_query = NccComplaintsReportQuery(start=start, end=end)
    records = build_records(db, start=start, end=end)
    by_category: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    unclassified = 0
    for record in records:
        category = record["Category"]
        by_status[record["Status"]] += 1
        if category:
            by_category[category] += 1
        else:
            unclassified += 1

    return NccComplaintsReportSnapshot(
        query=normalized_query,
        records=tuple(NccComplaintRecord.from_mapping(row) for row in records),
        by_category=tuple(sorted(by_category.items())),
        by_status=tuple(sorted(by_status.items())),
        # Non-zero means tickets reached the filing with no captured
        # classification: a capture gap, not a display problem.
        unclassified_count=unclassified,
    )


def build_report(db: Session, *, start: datetime, end: datetime) -> dict[str, Any]:
    """Compatibility projection for retained JSON and pack adapters."""

    return query_report(
        db, NccComplaintsReportQuery(start=start, end=end)
    ).as_legacy_dict()
