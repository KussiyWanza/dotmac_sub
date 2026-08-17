"""NCC complaints-return workbook: OOXML structure, filename, validation.

The workbook is what a compliance officer files, so the tests care about the
two things they rely on: that the file opens (valid zip, expected parts, the
hidden dropdown sheet the validation formulas point at) and that the
VALIDATION STATUS column tells the truth about a row.

Excel itself is not in the loop — we introspect the package with zipfile.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime

import pytest

from app.services import ncc_workbook


def _state_and_lga() -> tuple[str, str]:
    state = next(iter(ncc_workbook.STATE_LGAS))
    return state, ncc_workbook.STATE_LGAS[state][0]


def _valid_record() -> dict[str, str]:
    state, lga = _state_and_lga()
    return {
        "MSISDN": "2348031234567",
        "First Name": "Ada",
        "Last Name": "Obi",
        "Email": "ada@example.com",
        "Age": "34",
        "Gender": "Female",
        "created date time": "2026-07-01 09:00:00 UTC",
        "Subject": "Unexplained deduction",
        "Category": "Billing",
        "category code (auto)": "A",
        "sub category code": "A1 - Dropped Balance / Unexplained Deduction",
        "Ticket ID": "DOTMAC-20260701-1234",
        "Complaint type": "First Level",
        "Status": "Pending",
        "Language": "English",
        "Ticket source": "Phone Call",
        "State": state,
        "LGA": lga,
    }


def _workbook_parts(content: bytes) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(content))
    assert archive.testzip() is None, "workbook is a corrupt zip"
    return archive


# ── package structure ────────────────────────────────────────────────────────


def test_build_workbook_emits_a_valid_openable_package():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    assert isinstance(content, bytes)
    archive = _workbook_parts(content)
    assert set(archive.namelist()) == {
        "[Content_Types].xml",
        "_rels/.rels",
        "docProps/core.xml",
        "docProps/app.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/workbook.xml",
        "xl/styles.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
    }


def test_dropdown_sheet_is_hidden_and_backs_the_validation_formulas():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    archive = _workbook_parts(content)
    workbook = archive.read("xl/workbook.xml").decode()
    assert '<sheet name="_NCC_Dropdowns" sheetId="2" state="hidden"' in workbook

    sheet1 = archive.read("xl/worksheets/sheet1.xml").decode()
    # Every constrained column points at the hidden sheet, not an inline list.
    assert "'_NCC_Dropdowns'!" in sheet1
    assert "<dataValidations count=" in sheet1

    sheet2 = archive.read("xl/worksheets/sheet2.xml").decode()
    for value in ("Female", "Billing", "Resolved", "English"):
        assert value in sheet2


def test_required_dropdown_columns_disallow_blank():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet1 = _workbook_parts(content).read("xl/worksheets/sheet1.xml").decode()
    status_letter = ncc_workbook._COLUMN_LETTERS["Status"]
    validation = re.search(
        rf'<dataValidation type="list" allowBlank="(\d)"[^>]*sqref="{status_letter}2:',
        sheet1,
    )
    assert validation, "Status column has no list validation"
    assert validation.group(1) == "0", "Status is required — it must not allow blank"


def test_age_gets_a_custom_range_validation():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet1 = _workbook_parts(content).read("xl/worksheets/sheet1.xml").decode()
    assert 'type="custom"' in sheet1
    assert "&gt;=13" in sheet1 and "&lt;=150" in sheet1


def test_validation_extends_beyond_the_data_so_pasted_rows_stay_constrained():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet1 = _workbook_parts(content).read("xl/worksheets/sheet1.xml").decode()
    # sqref spans to row 1000 (e.g. Z2:Z1000) so pasted rows stay validated.
    assert re.search(r'sqref="[A-Z]+2:[A-Z]+1000"', sheet1)


def test_row_shading_follows_validation_outcome():
    ok = dict(_valid_record(), **{"VALIDATION STATUS": "[OK] All validations passed"})
    fail = dict(
        _valid_record(), **{"VALIDATION STATUS": "[FAIL] something is required"}
    )
    ok_sheet = (
        _workbook_parts(ncc_workbook.build_workbook([ok], ncc_workbook.COLUMNS))
        .read("xl/worksheets/sheet1.xml")
        .decode()
    )
    fail_sheet = (
        _workbook_parts(ncc_workbook.build_workbook([fail], ncc_workbook.COLUMNS))
        .read("xl/worksheets/sheet1.xml")
        .decode()
    )
    assert 's="10"' in ok_sheet, "passing rows shade green (style 10)"
    assert 's="11"' in fail_sheet, "failing rows shade red (style 11)"


def test_xml_special_characters_are_escaped():
    record = dict(_valid_record(), Subject='Bad & <ugly> "quote"')
    content = ncc_workbook.build_workbook([record], ncc_workbook.COLUMNS)
    sheet1 = _workbook_parts(content).read("xl/worksheets/sheet1.xml").decode()
    assert "Bad &amp; &lt;ugly&gt;" in sheet1


def test_empty_record_set_still_builds():
    content = ncc_workbook.build_workbook([], ncc_workbook.COLUMNS)
    archive = _workbook_parts(content)
    assert archive.read("xl/worksheets/sheet1.xml")


# ── filename ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "expected_week"),
    [(1, 1), (7, 1), (8, 2), (17, 3), (28, 4), (31, 5)],
)
def test_export_filename_week_matches_ncc_submission_format(day, expected_week):
    name = ncc_workbook.export_filename(datetime(2026, 7, day, tzinfo=UTC))
    assert name == f"Dotmac_Week{expected_week}_202607.xlsx"
    assert re.fullmatch(r"[A-Za-z]+_Week\d_\d{6}\.xlsx", name)


def test_export_filename_assumes_utc_for_naive_input():
    assert (
        ncc_workbook.export_filename(datetime(2026, 7, 17))
        == "Dotmac_Week3_202607.xlsx"
    )


def test_regulatory_pack_filename_spans_the_period():
    name = ncc_workbook.regulatory_pack_filename(
        datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC), "pdf"
    )
    assert name == "Dotmac_NCC_Regulatory_Pack_20260401_20260630.pdf"


# ── validation status ────────────────────────────────────────────────────────


def test_validation_passes_a_complete_record():
    assert (
        ncc_workbook.validation_status(_valid_record()) == "[OK] All validations passed"
    )


def test_validation_flags_conditionally_required_fields_when_resolved():
    """Status=Resolved makes three otherwise-optional columns mandatory."""
    record = dict(_valid_record(), Status="Resolved")
    status = ncc_workbook.validation_status(record)
    assert status.startswith("[FAIL]")
    for column in ("Resolved date", "Resolved within SLA", "Resolution Note"):
        assert f"{column} is required when Status is Resolved" in status


def test_resolved_record_with_its_conditional_fields_passes():
    record = dict(
        _valid_record(),
        Status="Resolved",
        **{
            "Resolved date": "2026-07-02 10:00:00 UTC",
            "Resolved within SLA": "Yes",
            "Resolution Note": "Refund applied to the account.",
        },
    )
    assert ncc_workbook.validation_status(record) == "[OK] All validations passed"


def test_data_depletion_requires_phone_type():
    record = dict(
        _valid_record(),
        Category="Data Depletion",
        **{"category code (auto)": "Q", "sub category code": ""},
    )
    status = ncc_workbook.validation_status(record)
    assert "Phone Type is required when Category is Data Depletion" in status


def test_validation_reports_the_excel_column_letter():
    record = dict(_valid_record(), Gender="")
    status = ncc_workbook.validation_status(record)
    assert f"(col {ncc_workbook._COLUMN_LETTERS['Gender']})" in status


def test_validation_rejects_msisdn_not_in_national_format():
    assert "MSISDN must start with 234" in ncc_workbook.validation_status(
        dict(_valid_record(), MSISDN="08031234567")
    )
    assert "MSISDN must be 13 digits including 234" in ncc_workbook.validation_status(
        dict(_valid_record(), MSISDN="23480312345")
    )


def test_validation_rejects_non_numeric_msisdn():
    status = ncc_workbook.validation_status(
        dict(_valid_record(), MSISDN="not-a-number")
    )
    assert status.startswith("[FAIL]")
    assert "MSISDN" in status


def test_blank_names_report_only_the_required_error():
    status = ncc_workbook.validation_status(
        dict(_valid_record(), **{"First Name": "", "Last Name": ""})
    )

    assert "First Name is required" in status
    assert "Last Name is required" in status
    assert "First Name must contain letters only" not in status
    assert "Last Name must contain letters only" not in status


def test_nonblank_malformed_names_still_report_format_errors():
    status = ncc_workbook.validation_status(
        dict(_valid_record(), **{"First Name": "Ada2", "Last Name": "Obi!"})
    )

    assert "First Name must contain letters only" in status
    assert "Last Name must contain letters only; hyphen is allowed" in status


def test_validation_rejects_test_data_in_names():
    status = ncc_workbook.validation_status(
        dict(_valid_record(), **{"First Name": "Test"})
    )
    assert "First Name must not contain test data" in status


def test_validation_accepts_na_placeholders_for_age_and_gender():
    record = dict(_valid_record(), Age="N/A", Gender="N/A")
    assert ncc_workbook.validation_status(record) == "[OK] All validations passed"


def test_validation_rejects_out_of_range_age():
    assert "Age must be N/A or a whole number" in ncc_workbook.validation_status(
        dict(_valid_record(), Age="9")
    )


def test_validation_rejects_ticket_id_in_the_wrong_format():
    assert "Ticket ID must use format DOTMAC-YYYYMMDD-Number" in (
        ncc_workbook.validation_status(dict(_valid_record(), **{"Ticket ID": "12345"}))
    )


def test_validation_rejects_subcategory_from_another_category():
    """A1 belongs to Billing — filing it under SMS / MMS is a mismatch."""
    record = dict(
        _valid_record(), Category="SMS / MMS", **{"category code (auto)": "J"}
    )
    status = ncc_workbook.validation_status(record)
    assert "sub category code must match the selected NCC category" in status


def test_subcategory_accepts_en_dash_separator():
    """NCC's own sheets mix hyphen and en-dash; both must resolve."""
    en_dash = "A1 – Dropped Balance / Unexplained Deduction"
    assert ncc_workbook.clean_subcategory_code(en_dash, category="Billing")


def test_placeholder_markers_do_not_satisfy_a_required_column():
    for placeholder in ("nil", "unknown", "-", "not specified"):
        status = ncc_workbook.validation_status(
            dict(_valid_record(), Language=placeholder)
        )
        assert "Language is required" in status, placeholder


# ── reference tables + rows ──────────────────────────────────────────────────


def test_category_table_carries_all_18_ncc_codes():
    assert len(ncc_workbook.CATEGORY_SLA) == 18
    codes = sorted(str(value["code"]) for value in ncc_workbook.CATEGORY_SLA.values())
    assert codes == list("ABCDEFGHIJKLMNOPQR")
    for value in ncc_workbook.CATEGORY_SLA.values():
        assert value["feedback_hours"] > 0 and value["resolution_hours"] > 0


def test_export_rows_drops_internal_styling_keys():
    rows = ncc_workbook.export_rows(
        [{"MSISDN": "2348031234567", "_status_variant": "success"}]
    )
    assert rows == [{"MSISDN": "2348031234567"}]


def test_column_widths_are_wider_for_long_text_columns():
    widths = ncc_workbook._export_column_widths([_valid_record()], ncc_workbook.COLUMNS)
    by_column = dict(zip(ncc_workbook.COLUMNS, widths, strict=True))
    assert by_column["Description (auto)"] == 42.0
    assert by_column["Resolution Note"] == 36.0
    assert by_column["Age"] == 10.0
    assert by_column["Description (auto)"] > by_column["Age"]


def test_excel_column_letters_roll_over_past_z():
    assert ncc_workbook._excel_column_letter(1) == "A"
    assert ncc_workbook._excel_column_letter(26) == "Z"
    assert ncc_workbook._excel_column_letter(27) == "AA"


def test_excel_serial_matches_the_1899_epoch():
    serial = ncc_workbook.excel_serial_from_display_timestamp("2026-07-01 00:00:00 UTC")
    assert serial == 46204.0
    assert ncc_workbook.excel_serial_from_display_timestamp("not a date") is None
    assert ncc_workbook.excel_serial_from_display_timestamp("") is None
