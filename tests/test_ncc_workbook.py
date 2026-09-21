"""NCC complaints-return artifact: CSV filing plus workbook validation.

The CSV is what a compliance officer files. The retained workbook checks still
prove the validation shell opens (valid zip, expected parts, the hidden Lookups
sheet the validation formulas point at) and that the VALIDATION STATUS column
tells the truth about a row.

Excel itself is not in the loop — we introspect the package with zipfile.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime

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
        "created date time": "01/07/2026 09:00:00",
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


def test_lookup_sheet_is_hidden_and_backs_the_validation_formulas():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    archive = _workbook_parts(content)
    workbook = archive.read("xl/workbook.xml").decode()
    assert (
        '<bookViews><workbookView firstSheet="1" activeTab="1"/></bookViews>'
        in workbook
    )
    assert '<sheet name="Lookups" sheetId="1" state="hidden"' in workbook
    assert '<sheet name="Data Entry" sheetId="2"' in workbook
    assert 'name="NCC_CATEGORIES"' in workbook
    assert 'name="CAT_A"' in workbook
    assert 'name="INTERNATIONAL"' in workbook

    sheet2 = archive.read("xl/worksheets/sheet2.xml").decode()
    assert "NCC_CATEGORIES" in sheet2
    assert 'INDIRECT("CAT_"&amp;$J2)' in sheet2
    assert 'INDIRECT(SUBSTITUTE($Y2," ","_"))' in sheet2
    assert "<dataValidations count=" in sheet2

    # The validated template splits its lists two ways: the long, formula-driven
    # ones live on the hidden Lookups sheet and are reached by defined name,
    # while the short fixed ones are inline literals on the validation itself.
    sheet1 = archive.read("xl/worksheets/sheet1.xml").decode()
    state, lga = _state_and_lga()
    for value in (next(iter(ncc_workbook.CATEGORY_SLA)), state, lga):
        assert value in sheet1, f"{value} should be backed by the Lookups sheet"
    for value in ("Female", "Resolved", "English"):
        assert value in sheet2, f"{value} should be an inline validation list"


def test_required_dropdown_columns_disallow_blank():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet2 = _workbook_parts(content).read("xl/worksheets/sheet2.xml").decode()
    status_letter = ncc_workbook._TEMPLATE_COLUMN_LETTERS["Status *"]
    validation = re.search(
        rf'<dataValidation type="list"[^>]*sqref="{status_letter}2:{status_letter}16001"',
        sheet2,
    )
    assert validation, "Status column has no list validation"
    assert "allowBlank" not in validation.group(0), (
        "Status is required; omitted allowBlank is Excel's false/default state"
    )


def test_workbook_headers_match_the_validated_ncc_template():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet2 = _workbook_parts(content).read("xl/worksheets/sheet2.xml").decode()

    for column in ncc_workbook.TEMPLATE_COLUMNS:
        assert f">{column}<" in sheet2
    assert '<row r="2">' in sheet2
    assert '<dimension ref="A1:AC15001"/>' in sheet2


def test_age_gets_a_custom_range_validation():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet2 = _workbook_parts(content).read("xl/worksheets/sheet2.xml").decode()
    assert 'type="whole"' in sheet2
    assert "<formula1>13</formula1>" in sheet2
    assert "<formula2>150</formula2>" in sheet2


def test_validation_extends_beyond_the_data_so_pasted_rows_stay_constrained():
    content = ncc_workbook.build_workbook([_valid_record()], ncc_workbook.COLUMNS)
    sheet2 = _workbook_parts(content).read("xl/worksheets/sheet2.xml").decode()
    assert '<dataValidations count="17">' in sheet2
    assert 'sqref="B2:C16001 M2:M16001"' in sheet2
    assert 'sqref="P2:P16001 T2:T16001"' in sheet2
    assert 'sqref="AB2:AB16001"' in sheet2
    assert not re.search(r'sqref="A2:A16001"', sheet2)
    assert not re.search(r'sqref="[A-Z]+4:[A-Z]+1048576"', sheet2)


def test_workbook_uses_latest_template_data_entry_shell():
    record = dict(
        _valid_record(),
        Status="Resolved",
        **{
            "Resolved date": "02/07/2026 10:00:00",
            "Resolved within SLA": "Yes",
            "Resolution Note": "Refund applied to the account.",
            "user notes datetime": "02/07/2026 10:05:00",
        },
    )
    content = ncc_workbook.build_workbook([record], ncc_workbook.COLUMNS)
    sheet2 = _workbook_parts(content).read("xl/worksheets/sheet2.xml").decode()

    assert "<autoFilter" not in sheet2
    assert 'topLeftCell="A2"' in sheet2
    assert '<c r="G2" s="4"><v>46204.375</v></c>' in sheet2
    assert '<c r="P2" s="4"><v>46205.4166667</v></c>' in sheet2
    assert '<c r="J2" s="9" t="str"><f>IF($I2=' in sheet2
    assert '<c r="L2" s="9" t="str"><f>IF($K2=' in sheet2


def test_row_shading_follows_validation_outcome():
    ok = dict(_valid_record(), **{"VALIDATION STATUS": "[OK] All validations passed"})
    fail = dict(
        _valid_record(), **{"VALIDATION STATUS": "[FAIL] something is required"}
    )
    ok_sheet = (
        _workbook_parts(ncc_workbook.build_workbook([ok], ncc_workbook.COLUMNS))
        .read("xl/worksheets/sheet2.xml")
        .decode()
    )
    fail_sheet = (
        _workbook_parts(ncc_workbook.build_workbook([fail], ncc_workbook.COLUMNS))
        .read("xl/worksheets/sheet2.xml")
        .decode()
    )
    assert 's="10"' in ok_sheet, "passing rows shade green (style 10)"
    assert 's="11"' in fail_sheet, "failing rows shade red (style 11)"


def test_xml_special_characters_are_escaped():
    record = dict(_valid_record(), Subject='Bad & <ugly> "quote"')
    content = ncc_workbook.build_workbook([record], ncc_workbook.COLUMNS)
    sheet2 = _workbook_parts(content).read("xl/worksheets/sheet2.xml").decode()
    assert "Bad &amp; &lt;ugly&gt;" in sheet2


def test_empty_record_set_still_builds():
    content = ncc_workbook.build_workbook([], ncc_workbook.COLUMNS)
    archive = _workbook_parts(content)
    assert archive.read("xl/worksheets/sheet2.xml")


# ── filename ─────────────────────────────────────────────────────────────────


def test_export_filename_week_matches_ncc_submission_format():
    name = ncc_workbook.export_filename(datetime(2026, 9, 6, tzinfo=UTC))
    assert name == "36_2026_COMPLAINTS_DOTMAC.csv"
    assert re.fullmatch(r"\d{2}_\d{4}_COMPLAINTS_DOTMAC\.csv", name)


def test_export_filename_for_window_uses_inclusive_reporting_end():
    name = ncc_workbook.export_filename_for_window(
        start=datetime(2026, 8, 31, tzinfo=UTC),
        end=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert name == "36_2026_COMPLAINTS_DOTMAC.csv"


def test_export_filename_assumes_utc_for_naive_input():
    assert (
        ncc_workbook.export_filename(datetime(2026, 9, 6))
        == "36_2026_COMPLAINTS_DOTMAC.csv"
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
            "Resolved date": "02/07/2026 10:00:00",
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


def test_validation_rejects_na_placeholders_for_age_and_gender():
    record = dict(_valid_record(), Age="N/A", Gender="N/A")
    status = ncc_workbook.validation_status(record)
    assert "Age must be a whole number from 13 to 150" in status
    assert "Gender must be Female or Male" in status


def test_validation_rejects_out_of_range_age():
    assert (
        "Age must be a whole number from 13 to 150"
        in ncc_workbook.validation_status(dict(_valid_record(), Age="9"))
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


def test_template_export_rows_uses_the_validated_template_headers():
    rows = ncc_workbook.template_export_rows([_valid_record()])

    assert list(rows[0]) == ncc_workbook.TEMPLATE_COLUMNS
    assert rows[0]["MSISDN *"] == "2348031234567"
    assert rows[0]["created_date_time *"] == "01/07/2026 09:00:00"
    assert rows[0]["Ticket_ID *"] == "DOTMAC-20260701-1234"
    assert "_status_variant" not in rows[0]


def test_build_csv_emits_one_provider_file_with_template_headers():
    rows = ncc_workbook.template_export_rows([_valid_record()])
    content = ncc_workbook.build_csv(rows, list(ncc_workbook.TEMPLATE_COLUMNS))

    text = content.decode("utf-8")
    assert text.splitlines()[0] == ",".join(ncc_workbook.TEMPLATE_COLUMNS)
    assert "2348031234567" in text
    assert "DOTMAC-20260701-1234" in text
    assert content[:4] != b"PK\x03\x04"


def test_column_widths_are_wider_for_long_text_columns():
    rows = ncc_workbook.template_export_rows([_valid_record()])
    widths = ncc_workbook._export_column_widths(rows, ncc_workbook.TEMPLATE_COLUMNS)
    by_column = dict(zip(ncc_workbook.TEMPLATE_COLUMNS, widths, strict=True))
    assert by_column["Description (auto)"] == 55.0
    assert by_column["Resolution Note"] == 45.0
    assert by_column["Age *"] == 8.0
    assert by_column["Description (auto)"] > by_column["Age *"]


def test_excel_column_letters_roll_over_past_z():
    assert ncc_workbook._excel_column_letter(1) == "A"
    assert ncc_workbook._excel_column_letter(26) == "Z"
    assert ncc_workbook._excel_column_letter(27) == "AA"


def test_excel_serial_matches_the_1899_epoch():
    serial = ncc_workbook.excel_serial_from_display_timestamp("01/07/2026 00:00:00")
    assert serial == 46204.0
    assert ncc_workbook.excel_serial_from_display_timestamp("not a date") is None
    assert ncc_workbook.excel_serial_from_display_timestamp("") is None
