"""NCC Quarterly Complaints return (①), built from native tickets.

The invariants worth protecting are the ones that keep a regulatory filing
honest: the classification is a *stored* decision an agent can correct, and
anything we cannot source is blank rather than invented.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta

from app.models.subscriber import Gender, Subscriber
from app.models.support import Ticket, TicketChannel
from app.schemas.support import TicketCreate, TicketUpdate
from app.services import ncc_categorisation, ncc_workbook
from app.services import ncc_complaints_report as report
from app.services import support as support_service


def _subscriber(db, **overrides) -> Subscriber:
    subscriber = Subscriber(
        first_name=overrides.pop("first_name", "Ada"),
        last_name=overrides.pop("last_name", "Obi"),
        email=overrides.pop("email", f"s-{uuid.uuid4().hex[:8]}@example.com"),
        phone=overrides.pop("phone", "2348030000000"),
        **overrides,
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _ticket(db, subscriber=None, **overrides) -> Ticket:
    ticket = Ticket(
        title=overrides.pop("title", "Internet is slow"),
        description=overrides.pop("description", "Speeds dropped this week"),
        status=overrides.pop("status", "open"),
        priority=overrides.pop("priority", "normal"),
        channel=overrides.pop("channel", TicketChannel.web),
        subscriber_id=subscriber.id if subscriber else None,
        created_at=overrides.pop("created_at", datetime.now(UTC) - timedelta(days=1)),
        **overrides,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _window():
    end = datetime.now(UTC)
    return end - timedelta(days=30), end


def _record_for(db, ticket) -> dict[str, str] | None:
    start, end = _window()
    for record in report.build_records(db, start=start, end=end):
        if record["Ticket ID"].endswith(str(ticket.number or ticket.id)):
            return record
    return None


# ── category: derived on save, stored, agent-correctable ────────────────────


def test_category_is_derived_and_stored_on_create(db_session):
    subscriber = _subscriber(db_session)
    ticket = support_service.Tickets.create(
        db_session,
        TicketCreate(
            title="Wrong invoice amount",
            description="I was charged twice",
            subscriber_id=subscriber.id,
            channel="web",
        ),
    )
    db_session.commit()
    assert ticket.ncc_category == "Billing"
    assert ticket.ncc_category_source == ncc_categorisation.SOURCE_DERIVED
    assert ticket.ncc_subcategory.startswith("A50 - ")
    assert ticket.ncc_subcategory_source == ncc_categorisation.SOURCE_DERIVED


def test_agent_correction_is_marked_and_never_re_derived(db_session):
    """The whole point of storing it: a human's decision must survive."""
    subscriber = _subscriber(db_session)
    ticket = support_service.Tickets.create(
        db_session,
        TicketCreate(
            title="Wrong invoice amount",
            description="I was charged twice",
            subscriber_id=subscriber.id,
            channel="web",
        ),
    )
    db_session.commit()
    assert ticket.ncc_category == "Billing"

    ticket = support_service.Tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(ncc_category="Failed Payment Transactions"),
    )
    db_session.commit()
    assert ticket.ncc_category == "Failed Payment Transactions"
    assert ticket.ncc_category_source == ncc_categorisation.SOURCE_AGENT

    # A later unrelated edit must not silently re-derive it back to Billing.
    ticket = support_service.Tickets.update(
        db_session, str(ticket.id), TicketUpdate(priority="high")
    )
    db_session.commit()
    assert ticket.ncc_category == "Failed Payment Transactions"
    assert ticket.ncc_category_source == ncc_categorisation.SOURCE_AGENT


def test_derived_category_tracks_later_text_edits(db_session):
    subscriber = _subscriber(db_session)
    ticket = support_service.Tickets.create(
        db_session,
        TicketCreate(title="Internet slow", subscriber_id=subscriber.id, channel="web"),
    )
    db_session.commit()
    assert ticket.ncc_category == "Quality of Service (Data)"

    ticket = support_service.Tickets.update(
        db_session, str(ticket.id), TicketUpdate(title="Wrong invoice charge")
    )
    db_session.commit()
    assert ticket.ncc_category == "Billing"
    assert ticket.ncc_category_source == ncc_categorisation.SOURCE_DERIVED


def test_report_reads_the_stored_category_not_the_text(db_session):
    """Proves the filing is a projection of a decision, not a fresh guess:
    text says Billing, the stored (agent) value says otherwise, and the stored
    value is what files."""
    subscriber = _subscriber(db_session)
    ticket = _ticket(
        db_session,
        subscriber,
        title="Wrong invoice amount",
        ncc_category="BTS Issues",
        ncc_category_source=ncc_categorisation.SOURCE_AGENT,
        ncc_subcategory="G50 - Others",
        ncc_subcategory_source=ncc_categorisation.SOURCE_AGENT,
    )
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["Category"] == "BTS Issues"
    assert record["category code (auto)"] == "G"


def test_unclassified_ticket_reports_blank_and_is_counted(db_session):
    """A ticket nothing classified must not be re-guessed at filing time; it
    reports blank and surfaces in unclassified_count."""
    subscriber = _subscriber(db_session)
    _ticket(db_session, subscriber, ncc_category=None)
    start, end = _window()
    built = report.build_report(db_session, start=start, end=end)
    assert built["unclassified_count"] == 1
    assert built["records"][0]["Category"] == ""


# ── status map ──────────────────────────────────────────────────────────────


def test_closed_status_files_in_ncc_resolved_vocabulary(db_session):
    """Closed is native; Resolved is the NCC filing's external vocabulary."""
    subscriber = _subscriber(db_session)
    ticket = _ticket(
        db_session,
        subscriber,
        status="closed",
        resolved_at=datetime.now(UTC) - timedelta(hours=1),
        ncc_category="Billing",
        ncc_category_source=ncc_categorisation.SOURCE_DERIVED,
    )
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["Status"] == "Resolved"


def test_open_status_files_as_pending(db_session):
    subscriber = _subscriber(db_session)
    ticket = _ticket(db_session, subscriber, status="waiting_on_customer")
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["Status"] == "Pending"


def test_canceled_and_merged_are_excluded_as_non_complaints(db_session):
    subscriber = _subscriber(db_session)
    _ticket(db_session, subscriber, status="canceled")
    _ticket(db_session, subscriber, status="merged")
    _ticket(db_session, subscriber, status="open")
    start, end = _window()
    records = report.build_records(db_session, start=start, end=end)
    assert len(records) == 1
    assert records[0]["Status"] == "Pending"


# ── SLA: unknown is blank, not a breach ─────────────────────────────────────


def test_null_due_at_reports_blank_sla_not_a_breach(db_session):
    """CRM returned "No" — filing a breach it had no evidence for."""
    subscriber = _subscriber(db_session)
    ticket = _ticket(
        db_session,
        subscriber,
        status="closed",
        closed_at=datetime.now(UTC) - timedelta(hours=1),
        due_at=None,
    )
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["Status"] == "Resolved"
    assert record["Resolved within SLA"] == ""


def test_sla_yes_and_no_when_due_at_is_known(db_session):
    subscriber = _subscriber(db_session)
    resolved_at = datetime.now(UTC) - timedelta(hours=2)
    in_time = _ticket(
        db_session,
        subscriber,
        status="closed",
        resolved_at=resolved_at,
        due_at=resolved_at + timedelta(hours=1),
    )
    late = _ticket(
        db_session,
        subscriber,
        status="closed",
        resolved_at=resolved_at,
        due_at=resolved_at - timedelta(hours=1),
    )
    assert _record_for(db_session, in_time)["Resolved within SLA"] == "Yes"
    assert _record_for(db_session, late)["Resolved within SLA"] == "No"


# ── location: never invented ────────────────────────────────────────────────


def test_unresolvable_location_reports_blank_not_fct(db_session):
    """CRM defaulted an unmatched address to Municipal Area Council, FCT — an
    unlocatable complainant became an Abuja statistic."""
    subscriber = _subscriber(db_session, region=None, city=None)
    ticket = _ticket(db_session, subscriber, region="Nowhere in particular")
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["State"] == ""
    assert record["LGA"] == ""
    assert record["Town"] == ""


def test_resolvable_state_is_reported_and_canonicalised(db_session):
    subscriber = _subscriber(db_session, region="Lagos")
    ticket = _ticket(db_session, subscriber)
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["State"] == "LAGOS"
    assert record["State"] in ncc_workbook.STATE_LGAS


def test_fct_district_canonicalises_to_its_area_council(db_session):
    """Deriving "Municipal Area Council" from a written "Wuse" is a lookup in
    NCC's own table, not a guess about where someone lives."""
    subscriber = _subscriber(db_session, region="Federal Capital Territory")
    ticket = _ticket(db_session, subscriber, region="Wuse")
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["State"] == "FEDERAL CAPITAL TERRITORY"
    assert record["LGA"] == "Municipal Area Council"
    assert record["Town"] == "Wuse"


# ── gaps reported honestly ──────────────────────────────────────────────────


def test_alt_phone_is_blank_because_sub_has_no_source(db_session):
    subscriber = _subscriber(db_session)
    ticket = _ticket(db_session, subscriber)
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["alt phone number"] == ""


def test_age_and_gender_come_from_the_subscriber_or_report_na(db_session):
    known = _subscriber(
        db_session, date_of_birth=date(1990, 1, 1), gender=Gender.female
    )
    known_ticket = _ticket(db_session, known)
    known_record = _record_for(db_session, known_ticket)
    assert known_record["Age"].isdigit()
    assert known_record["Gender"] == "Female"

    unlinked_ticket = _ticket(db_session, None)
    unlinked_record = _record_for(db_session, unlinked_ticket)
    assert unlinked_record["Age"] == "N/A"
    assert unlinked_record["Gender"] == "N/A"


# ── the record must survive the workbook ────────────────────────────────────


def test_record_round_trips_into_the_filed_workbook(db_session):
    subscriber = _subscriber(db_session, region="Lagos")
    _ticket(
        db_session,
        subscriber,
        ncc_category="Billing",
        ncc_category_source=ncc_categorisation.SOURCE_AGENT,
        ncc_subcategory="A50 - Others",
        ncc_subcategory_source=ncc_categorisation.SOURCE_AGENT,
    )
    start, end = _window()
    built = report.build_report(db_session, start=start, end=end)
    assert built["columns"] == list(ncc_workbook.COLUMNS)

    rows = ncc_workbook.export_rows(built["records"])
    workbook = ncc_workbook.build_workbook(rows, list(ncc_workbook.COLUMNS))
    assert workbook[:2] == b"PK"  # a real xlsx package
    assert all("VALIDATION STATUS" in row for row in rows)


def test_report_totals_agree_with_records(db_session):
    subscriber = _subscriber(db_session)
    _ticket(db_session, subscriber, status="open")
    _ticket(
        db_session,
        subscriber,
        status="closed",
        resolved_at=datetime.now(UTC) - timedelta(hours=1),
    )
    start, end = _window()
    built = report.build_report(db_session, start=start, end=end)
    assert built["total_complaints"] == len(built["records"]) == 2
    assert sum(built["by_status"].values()) == 2


# ── LGA: captured, validated against its state, never derived ───────────────


def test_captured_lga_is_reported(db_session):
    """The point of the column: a captured LGA reaches the filing."""
    subscriber = _subscriber(db_session, region="Lagos", lga="Eti-Osa")
    ticket = _ticket(db_session, subscriber)
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["State"] == "LAGOS"
    assert record["LGA"] == "Eti-Osa"


def test_captured_lga_clears_the_workbook_lga_failure(db_session):
    """End-to-end proof. Without a captured LGA the export tells the officer
    the row is not filable; with one, that specific failure is gone."""
    without = _subscriber(db_session, region="Lagos")
    blank_record = _record_for(db_session, _ticket(db_session, without))
    assert blank_record is not None
    assert "LGA is required" in ncc_workbook.validation_status(blank_record)

    with_lga = _subscriber(db_session, region="Lagos", lga="Eti-Osa")
    filled_record = _record_for(db_session, _ticket(db_session, with_lga))
    assert filled_record is not None
    assert "LGA is required" not in ncc_workbook.validation_status(filled_record)


def test_lga_stranded_by_a_state_change_reports_blank(db_session):
    """Capture-time validation cannot bind forever: a later region change can
    strand a valid-for-the-old-state LGA. Filing "Eti-Osa, Kano" would be a
    wrong return, so the report re-checks and reports blank."""
    subscriber = _subscriber(db_session, region="Kano", lga="Eti-Osa")
    ticket = _ticket(db_session, subscriber)
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["State"] == "KANO"
    assert record["LGA"] == ""


def test_uncaptured_lga_is_blank_never_guessed(db_session):
    """A resolvable state does not license guessing the LGA within it."""
    subscriber = _subscriber(db_session, region="Lagos", lga=None)
    ticket = _ticket(db_session, subscriber)
    record = _record_for(db_session, ticket)
    assert record is not None
    assert record["State"] == "LAGOS"
    assert record["LGA"] == ""


def test_captured_lga_round_trips_through_the_workbook(db_session):
    subscriber = _subscriber(db_session, region="Lagos", lga="Eti-Osa")
    ticket = _ticket(db_session, subscriber)
    record = _record_for(db_session, ticket)
    assert record is not None
    content = ncc_workbook.build_workbook(
        ncc_workbook.export_rows([record]), ncc_workbook.COLUMNS
    )
    # The workbook is a zip: the value is deflated, so it never appears
    # literally in the archive bytes — read the sheet out and look there.
    with zipfile.ZipFile(io.BytesIO(content)) as parts:
        sheet1 = parts.read("xl/worksheets/sheet1.xml").decode()
    assert "Eti-Osa" in sheet1
