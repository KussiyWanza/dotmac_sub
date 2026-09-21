"""NCC complaints-return artifact: the CSV a compliance officer files.

Ported from dotmac_crm's ``app/web/admin/reports.py`` for the CRM exit — CRM
owns NCC return ① (Quarterly Complaints) today and leaves the operation, so
the filing artifact comes with it. The builder is pure: it takes
already-derived records and emits bytes, touching no models. It still carries
the validated workbook builder for operator review, but the provider filing
artifact is a single CSV file.

What the officer relies on, preserved exactly from CRM:

* **Dropdowns** on every constrained column, sourced from the hidden NCC
  ``Lookups`` sheet, so a filing cannot carry a value NCC rejects.
* **A per-row VALIDATION STATUS** column reading ``[OK] All validations
  passed`` or ``[FAIL] <reason>; <reason>`` with the offending Excel column
  letters, plus green/red row shading — the officer fixes the reds and files.

The reference tables below (categories + per-category SLA hours, 87
subcategory issue codes, 38 states and their LGAs) are the regulator's
vocabulary. They are exported publicly because the records builder needs the
same vocabulary to derive rows; if a third consumer appears, lift them into
their own ``ncc_reference`` module rather than copying them.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape  # nosec B406 — escaping XML output, never parsing
from zipfile import ZIP_DEFLATED, ZipFile

# TODO: operator identity is per-deployment; promote to a setting when a
# second operator needs this return. Hardcoded in CRM too.
OPERATOR_NAME = "Dotmac"
OPERATOR_PREFIX = "DOTMAC"
EXPORT_TITLE = "Dotmac NCC Report"
CSV_CONTENT_TYPE = "text/csv; charset=utf-8"

COLUMNS = [
    "MSISDN",
    "First Name",
    "Last Name",
    "Email",
    "Age",
    "Gender",
    "created date time",
    "Subject",
    "Category",
    "category code (auto)",
    "sub category code",
    "Description (auto)",
    "Ticket ID",
    "Complaint type",
    "Status",
    "Resolved date",
    "Resolved within SLA",
    "Resolution Note",
    "User Note",
    "user notes datetime",
    "Language",
    "Ticket source",
    "alt phone number",
    "created by",
    "State",
    "LGA",
    "Town",
    "Phone Type",
    "VALIDATION STATUS",
]

TEMPLATE_COLUMNS = [
    "MSISDN *",
    "First Name *",
    "Last Name *",
    "Email",
    "Age *",
    "Gender *",
    "created_date_time *",
    "Subject",
    "Category *",
    "category_code (auto)",
    "sub_category_code *",
    "Description (auto)",
    "Ticket_ID *",
    "Complaint_type *",
    "Status *",
    "Resolved_date",
    "Resolved within SLA",
    "Resolution Note",
    "User_Note",
    "user_notes_datetime",
    "Language *",
    "Ticket_source *",
    "alt_phone_number",
    "created_by",
    "State *",
    "LGA *",
    "Town",
    "Phone Type",
    "VALIDATION STATUS",
]

TEMPLATE_COLUMN_BY_INTERNAL = dict(zip(COLUMNS, TEMPLATE_COLUMNS, strict=True))
INTERNAL_COLUMN_BY_TEMPLATE = {
    template: internal for internal, template in TEMPLATE_COLUMN_BY_INTERNAL.items()
}

CATEGORY_SLA: dict[str, dict[str, int | str]] = {
    "Billing": {"code": "A", "feedback_hours": 4, "resolution_hours": 24},
    "Call Center / Customer Care": {
        "code": "B",
        "feedback_hours": 4,
        "resolution_hours": 24,
    },
    "Quality of Service (Voice)": {
        "code": "C",
        "feedback_hours": 4,
        "resolution_hours": 72,
    },
    "Quality of Service (Data)": {
        "code": "D",
        "feedback_hours": 4,
        "resolution_hours": 72,
    },
    "Quality of Experience": {"code": "E", "feedback_hours": 4, "resolution_hours": 72},
    "Faulty Terminals": {"code": "F", "feedback_hours": 48, "resolution_hours": 72},
    "BTS Issues": {"code": "G", "feedback_hours": 72, "resolution_hours": 720},
    "Sales Promotions & Advertisement": {
        "code": "H",
        "feedback_hours": 2,
        "resolution_hours": 24,
    },
    "Recharge / Top-Up Issues": {
        "code": "I",
        "feedback_hours": 4,
        "resolution_hours": 24,
    },
    "SMS / MMS": {"code": "J", "feedback_hours": 4, "resolution_hours": 24},
    "Other SIM-Related Issues": {
        "code": "K",
        "feedback_hours": 4,
        "resolution_hours": 24,
    },
    "SIM Replacement": {"code": "L", "feedback_hours": 2, "resolution_hours": 12},
    "Value-Added Services (VAS)": {
        "code": "M",
        "feedback_hours": 4,
        "resolution_hours": 24,
    },
    "Mobile Number Portability (MNP)": {
        "code": "N",
        "feedback_hours": 4,
        "resolution_hours": 24,
    },
    "Do-Not-Disturb (DND) Service": {
        "code": "O",
        "feedback_hours": 4,
        "resolution_hours": 12,
    },
    "International Roaming": {"code": "P", "feedback_hours": 4, "resolution_hours": 72},
    "Data Depletion": {"code": "Q", "feedback_hours": 4, "resolution_hours": 24},
    "Failed Payment Transactions": {
        "code": "R",
        "feedback_hours": 2,
        "resolution_hours": 12,
    },
}

SUBCATEGORY_ROWS: tuple[dict[str, object], ...] = (
    {
        "category": "Billing",
        "issue_code": "A1",
        "name": "Dropped Balance / Unexplained Deduction",
        "description": "Unexplained balance change, overcharging, silent-call charges, undelivered SMS charges, or recharge not reflecting.",
    },
    {
        "category": "Billing",
        "issue_code": "A2",
        "name": "Inability to Change Tariff Plan",
        "description": "Consumer is unable to migrate from one tariff plan to another.",
    },
    {
        "category": "Billing",
        "issue_code": "A3",
        "name": "Suspension of Postpaid Line",
        "description": "Consumer line is suspended due to a disputed bill.",
    },
    {
        "category": "Billing",
        "issue_code": "A4",
        "name": "Renewal of Data Subscription",
        "description": "Consumer is unable to renew a data subscription.",
    },
    {
        "category": "Billing",
        "issue_code": "A5",
        "name": "Reduction in Validity Period",
        "description": "Consumer data validity period is reduced arbitrarily.",
    },
    {
        "category": "Billing",
        "issue_code": "A6",
        "name": "Data Subscription Not Rolled Over",
        "description": "Consumer is unable to roll over unused data.",
    },
    {
        "category": "Billing",
        "issue_code": "A50",
        "name": "Others (Billing)",
        "description": "Any other billing related issue not captured above.",
    },
    {
        "category": "Call Center / Customer Care",
        "issue_code": "B1",
        "name": "Inability to Connect to Call Center Helpline",
        "description": "Consumer is unable to connect to the service provider customer care helpline.",
    },
    {
        "category": "Call Center / Customer Care",
        "issue_code": "B2",
        "name": "Downtime of Service Provider's Call Centre",
        "description": "Service provider call center is not operational.",
    },
    {
        "category": "Call Center / Customer Care",
        "issue_code": "B3",
        "name": "Poor Customer Service",
        "description": "Consumer is poorly attended to by a customer care representative or call center agent.",
    },
    {
        "category": "Call Center / Customer Care",
        "issue_code": "B4",
        "name": "Incorrect Responses / Information from Agents",
        "description": "Consumer is given wrong or misleading information by customer care representatives.",
    },
    {
        "category": "Call Center / Customer Care",
        "issue_code": "B5",
        "name": "Inability to Connect to Live Agent",
        "description": "Consumer is unable to connect to a live agent within the expected timeframe.",
    },
    {
        "category": "Call Center / Customer Care",
        "issue_code": "B50",
        "name": "Others (Customer Care)",
        "description": "Any other customer care related issue not captured above.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C1",
        "name": "Call Interference / Voice Clarity / Background Noise",
        "description": "Consumer cannot clearly hear during a call or experiences call interference/background noise.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C2",
        "name": "Inability to Receive Calls",
        "description": "Consumer cannot receive calls from same network, other networks, or outside the country.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C3",
        "name": "Inability to Make Calls",
        "description": "Consumer cannot make successful calls within or outside the network/country despite sufficient airtime.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C4",
        "name": "Call Divert Issues",
        "description": "Consumer is unable to activate call divert.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C5",
        "name": "Unauthorized Call Divert Activation",
        "description": "Call divert is activated on the consumer line without request.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C6",
        "name": "Call Barring",
        "description": "Consumer is prohibited from making or receiving calls for a period due to network-related barring.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C7",
        "name": "Poor Signal",
        "description": "Consumer has poor reception at a particular location.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C8",
        "name": "No Network",
        "description": "Consumer does not have network reception.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C9",
        "name": "Dropped Call",
        "description": "Consumer call is abruptly disconnected and cannot successfully complete.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C10",
        "name": "Call Crossing (Wrong Routing)",
        "description": "A call is routed to a wrong person or line.",
    },
    {
        "category": "Quality of Service (Voice)",
        "issue_code": "C50",
        "name": "Others (QoS Voice)",
        "description": "Any other call setup or voice quality issue not captured above.",
    },
    {
        "category": "Quality of Service (Data)",
        "issue_code": "D1",
        "name": "Poor Internet Service",
        "description": "Consumer experiences poor internet service.",
    },
    {
        "category": "Quality of Service (Data)",
        "issue_code": "D2",
        "name": "No Internet Network",
        "description": "Consumer does not have internet service.",
    },
    {
        "category": "Quality of Service (Data)",
        "issue_code": "D3",
        "name": "Low / Poor Internet Speed",
        "description": "Consumer has low or poor internet speed.",
    },
    {
        "category": "Quality of Service (Data)",
        "issue_code": "D4",
        "name": "Others (QoS Data)",
        "description": "Any other data related issue not captured above.",
    },
    {
        "category": "Quality of Experience",
        "issue_code": "E1",
        "name": "Call Masking and Refiling",
        "description": "Consumer receives an international call showing a local number.",
    },
    {
        "category": "Quality of Experience",
        "issue_code": "E2",
        "name": "Disconnection of Internet Services",
        "description": "Consumer internet service is disconnected.",
    },
    {
        "category": "Quality of Experience",
        "issue_code": "E3",
        "name": "Installation of Internet Services Equipment",
        "description": "Consumer internet service equipment is not installed at the agreed time.",
    },
    {
        "category": "Quality of Experience",
        "issue_code": "E4",
        "name": "Others (Quality of Experience)",
        "description": "Any other quality of experience issue not captured above.",
    },
    {
        "category": "Faulty Terminals",
        "issue_code": "F1",
        "name": "Faulty Terminals (Phones, Routers, Modems)",
        "description": "Consumer has problems with phones, routers, modems, or other terminals.",
    },
    {
        "category": "Faulty Terminals",
        "issue_code": "F50",
        "name": "Others (Faulty Terminals)",
        "description": "Any other faulty terminal related issue not captured above.",
    },
    {
        "category": "BTS Issues",
        "issue_code": "G1",
        "name": "Base Station Issues",
        "description": "Problems arising from installation or location of a base station, mast, or tower.",
    },
    {
        "category": "BTS Issues",
        "issue_code": "G2",
        "name": "Pollution from BTS Site / Generator",
        "description": "Consumer complains of environmental pollution from a BTS site generator.",
    },
    {
        "category": "BTS Issues",
        "issue_code": "G50",
        "name": "Others (BTS)",
        "description": "Any other BTS related issue not captured above.",
    },
    {
        "category": "Sales Promotions & Advertisement",
        "issue_code": "H1",
        "name": "Bonus / Promotions Issues",
        "description": "Consumer does not receive promotion bonus/incentive or receives misleading/incomplete offer information.",
    },
    {
        "category": "Sales Promotions & Advertisement",
        "issue_code": "H50",
        "name": "Others (Promotions)",
        "description": "Any other promotion related issue not captured above.",
    },
    {
        "category": "Recharge / Top-Up Issues",
        "issue_code": "I1",
        "name": "Mutilated Vouchers",
        "description": "Consumer is unable to identify numbers on a voucher.",
    },
    {
        "category": "Recharge / Top-Up Issues",
        "issue_code": "I2",
        "name": "Recharge Barring",
        "description": "Consumer is barred from recharging after several wrong attempts.",
    },
    {
        "category": "Recharge / Top-Up Issues",
        "issue_code": "I3",
        "name": "Inability to Check Airtime / Data Balance",
        "description": "Consumer cannot check data or airtime balance via USSD or IVR.",
    },
    {
        "category": "Recharge / Top-Up Issues",
        "issue_code": "I5",
        "name": "Invalid Voucher",
        "description": "Consumer purchases an invalid voucher or receives an invalid prompt when loading a voucher.",
    },
    {
        "category": "Recharge / Top-Up Issues",
        "issue_code": "I6",
        "name": "Over Recharge",
        "description": "Consumer recharges over the intended value where resolution is not third-party dependent.",
    },
    {
        "category": "Recharge / Top-Up Issues",
        "issue_code": "I50",
        "name": "Others (Recharge)",
        "description": "Any other recharge/top-up related issue not captured above.",
    },
    {
        "category": "SMS / MMS",
        "issue_code": "J1",
        "name": "Inability to Send SMS",
        "description": "Consumer is unable to send SMS or is charged for SMS that is not delivered.",
    },
    {
        "category": "SMS / MMS",
        "issue_code": "J2",
        "name": "Inability to Receive SMS",
        "description": "Consumer is unable to receive SMS locally or from outside the country.",
    },
    {
        "category": "SMS / MMS",
        "issue_code": "J3",
        "name": "MMS Charges (Undelivered MMS)",
        "description": "Consumer is charged for undelivered MMS.",
    },
    {
        "category": "SMS / MMS",
        "issue_code": "J50",
        "name": "Others (SMS/MMS)",
        "description": "Any other SMS/MMS related issue not captured above.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K1",
        "name": "Request for SIM Block",
        "description": "Consumer requests that a SIM be blocked.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K2",
        "name": "SIM Blocked - PUK Required",
        "description": "Consumer requires PUK from the service provider to unblock a SIM.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K3",
        "name": "Unauthorized Suspension of Mobile Line",
        "description": "Consumer line is wrongfully suspended by the service provider.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K4",
        "name": "SIM Registration (Incorrect Details)",
        "description": "Consumer SIM registration details are incorrect.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K5",
        "name": "Incomplete SIM Registration",
        "description": "Consumer is asked to re-register a SIM.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K6",
        "name": "NIN-SIM Linkage Issues",
        "description": "Consumer NIN is not successfully linked by the service provider.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K7",
        "name": "Inactive SIM",
        "description": "Consumer SIM is barred, suspended, or deactivated due to inactivity/NIN-SIM linkage or in error.",
    },
    {
        "category": "Other SIM-Related Issues",
        "issue_code": "K50",
        "name": "Others (SIM-Related)",
        "description": "Any other SIM related issue not captured above.",
    },
    {
        "category": "SIM Replacement",
        "issue_code": "L1",
        "name": "Fraudulent / Unauthorized SIM Swap",
        "description": "Consumer SIM is reported swapped without consent.",
    },
    {
        "category": "SIM Replacement",
        "issue_code": "L2",
        "name": "Inactive SIM Replacement",
        "description": "SIM replacement is completed but the SIM remains inactive.",
    },
    {
        "category": "SIM Replacement",
        "issue_code": "L3",
        "name": "Retrieval of Deceased Relative's SIM",
        "description": "Consumer cannot complete SIM replacement for a deceased relative after providing requirements.",
    },
    {
        "category": "SIM Replacement",
        "issue_code": "L50",
        "name": "Others (SIM Replacement)",
        "description": "Any other SIM swap/replacement related issue.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M1",
        "name": "Inability to Activate / Deactivate VAS",
        "description": "Consumer is unable to opt in or opt out of VAS services.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M2",
        "name": "VAS Charges (Unrendered / Wrong Service)",
        "description": "Consumer is charged for VAS not rendered or receives the wrong VAS.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M3",
        "name": "Forceful Activation of VAS",
        "description": "Consumer is opted into VAS without consent.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M4",
        "name": "Inability to Listen to Voice SMS / Voicemail",
        "description": "Consumer is unable to listen to Voice SMS from the service provider network.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M5",
        "name": "Inability to Access / Activate Voice SMS",
        "description": "Consumer is unable to send Voice SMS.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M6",
        "name": "Inability to Access Voice Mail",
        "description": "Consumer is unable to recover voicemail.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M7",
        "name": "Failed Voice SMS",
        "description": "Consumer is charged for Voice SMS that is not delivered.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M8",
        "name": "Inability to Activate / Deactivate Voicemail Box",
        "description": "Consumer is unable to deactivate or activate voicemail.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M9",
        "name": "Voicemail Password Reset / Retrieval",
        "description": "Consumer is unable to change or recover voicemail password.",
    },
    {
        "category": "Value-Added Services (VAS)",
        "issue_code": "M50",
        "name": "Others (VAS)",
        "description": "Any other VAS related issue not captured above.",
    },
    {
        "category": "Mobile Number Portability (MNP)",
        "issue_code": "N1",
        "name": "Porting Issues",
        "description": "Consumer is unable to successfully port from one service provider to another within the porting timeline.",
    },
    {
        "category": "Mobile Number Portability (MNP)",
        "issue_code": "N50",
        "name": "Others (MNP)",
        "description": "Any other MNP related issue not captured above.",
    },
    {
        "category": "Do-Not-Disturb (DND) Service",
        "issue_code": "O1",
        "name": "Inability to Opt In / Out of DND",
        "description": "Consumer is unable to opt in or out of DND fully or partially.",
    },
    {
        "category": "Do-Not-Disturb (DND) Service",
        "issue_code": "O2",
        "name": "Receipt of Unsolicited SMS / Calls After Full DND",
        "description": "Consumer continues to receive unsolicited SMS/calls after activating full DND.",
    },
    {
        "category": "Do-Not-Disturb (DND) Service",
        "issue_code": "O50",
        "name": "Others (DND)",
        "description": "Any other DND related issue not captured above.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P1",
        "name": "Inability to Send / Receive SMS While Roaming",
        "description": "Consumer is unable to send or receive SMS while outside the country.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P2",
        "name": "Inability to Make / Receive Calls While Roaming",
        "description": "Consumer is unable to make or receive calls while outside the country.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P3",
        "name": "Inability to Roam",
        "description": "Consumer is unable to roam.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P4",
        "name": "Internet Service Not Working While Roaming",
        "description": "Consumer is unable to browse while outside the country.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P5",
        "name": "Inability to Recharge While Roaming",
        "description": "Consumer is unable to recharge while outside the country.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P6",
        "name": "Overcharged While Roaming",
        "description": "Consumer is overcharged for calls/data while outside the country.",
    },
    {
        "category": "International Roaming",
        "issue_code": "P50",
        "name": "Others (Roaming)",
        "description": "Any other roaming related issue.",
    },
    {
        "category": "Data Depletion",
        "issue_code": "Q1",
        "name": "Data Depletion",
        "description": "Consumer data gets used up or exhausted faster than expected.",
    },
    {
        "category": "Data Depletion",
        "issue_code": "Q50",
        "name": "Others (Data Depletion)",
        "description": "Any other data depletion related issue.",
    },
    {
        "category": "Failed Payment Transactions",
        "issue_code": "R1",
        "name": "Inability to Recharge / Failed Sharing / Mobile App",
        "description": "Consumer cannot purchase airtime/data via IVR/USSD, is debited for failed sharing, or is charged for failed third-party/mobile app top-up.",
    },
    {
        "category": "Failed Payment Transactions",
        "issue_code": "R50",
        "name": "Others (Failed Payment Transactions)",
        "description": "Any other failed payment transaction related issue.",
    },
)

REQUIRED_COLUMNS = {
    "MSISDN",
    "First Name",
    "Last Name",
    "Age",
    "Gender",
    "created date time",
    "Category",
    "sub category code",
    "Ticket ID",
    "Complaint type",
    "Status",
    "Language",
    "Ticket source",
    "State",
    "LGA",
}

REQUIRED_DROPDOWN_COLUMNS = {
    "Gender",
    "Gender *",
    "Category",
    "Category *",
    "sub category code",
    "sub_category_code *",
    "Complaint type",
    "Complaint_type *",
    "Status",
    "Status *",
    "Language",
    "Language *",
    "Ticket source",
    "Ticket_source *",
    "State",
    "State *",
    "LGA",
    "LGA *",
}

STATE_LGAS: dict[str, tuple[str, ...]] = {
    "ABIA": (
        "Aba North",
        "Aba South",
        "Arochukwu",
        "Bende",
        "Ikwuano",
        "Isiala-Ngwa North",
        "Isiala-Ngwa South",
        "Isuikwuato",
        "Obi Ngwa",
        "Ohafia",
        "Osisioma Ngwa",
        "Ugwunagbo",
        "Ukwa East",
        "Ukwa West",
        "Umuahia North",
        "Umuahia South",
        "Umu-Nneochi",
    ),
    "ADAMAWA": (
        "Demsa",
        "Fufore",
        "Ganye",
        "Girei",
        "Gombi",
        "Guyuk",
        "Hong",
        "Jada",
        "Lamurde",
        "Madagali",
        "Maiha",
        "Mayo-Belwa",
        "Michika",
        "Mubi North",
        "Mubi South",
        "Numan",
        "Shelleng",
        "Song",
        "Toungo",
        "Yola North",
        "Yola South",
    ),
    "AKWA IBOM": (
        "Abak",
        "Eastern Obolo",
        "Eket",
        "Esit-Eket",
        "Essien Udim",
        "Etim Ekpo",
        "Etinan",
        "Ibeno",
        "Ibesikpo Asutan",
        "Ibiono-Ibom",
        "Ika",
        "Ikono",
        "Ikot Abasi",
        "Ikot Ekpene",
        "Ini",
        "Itu",
        "Mbo",
        "Mkpat-Enin",
        "Nsit-Atai",
        "Nsit-Ibom",
        "Nsit-Ubium",
        "Obot Akara",
        "Okobo",
        "Onna",
        "Oron",
        "Oruk Anam",
        "Udung-Uko",
        "Ukanafun",
        "Uruan",
        "Urue-Offong/Oruko",
        "Uyo",
    ),
    "ANAMBRA": (
        "Aguata",
        "Anambra East",
        "Anambra West",
        "Anaocha",
        "Awka North",
        "Awka South",
        "Ayamelum",
        "Dunukofia",
        "Ekwusigo",
        "Idemili North",
        "Idemili South",
        "Ihiala",
        "Njikoka",
        "Nnewi North",
        "Nnewi South",
        "Ogbaru",
        "Onitsha North",
        "Onitsha South",
        "Orumba North",
        "Orumba South",
        "Oyi",
    ),
    "BAUCHI": (
        "Alkaleri",
        "Bauchi",
        "Bogoro",
        "Damban",
        "Darazo",
        "Dass",
        "Gamawa",
        "Ganjuwa",
        "Giade",
        "Itas/Gadau",
        "Jama'are",
        "Katagum",
        "Kirfi",
        "Misau",
        "Ningi",
        "Shira",
        "Tafawa Balewa",
        "Toro",
        "Warji",
        "Zaki",
    ),
    "BAYELSA": (
        "Brass",
        "Ekeremor",
        "Kolokuma/Opokuma",
        "Nembe",
        "Ogbia",
        "Sagbama",
        "Southern Ijaw",
        "Yenagoa",
    ),
    "BENUE": (
        "Ado",
        "Agatu",
        "Apa",
        "Buruku",
        "Gboko",
        "Guma",
        "Gwer East",
        "Gwer West",
        "Katsina-Ala",
        "Konshisha",
        "Kwande",
        "Logo",
        "Makurdi",
        "Obi",
        "Ogbadibo",
        "Ohimini",
        "Oju",
        "Okpokwu",
        "Otukpo",
        "Tarka",
        "Ukum",
        "Ushongo",
        "Vandeikya",
    ),
    "BORNO": (
        "Abadam",
        "Askira/Uba",
        "Bama",
        "Bayo",
        "Biu",
        "Chibok",
        "Damboa",
        "Dikwa",
        "Gubio",
        "Guzamala",
        "Gwoza",
        "Hawul",
        "Jere",
        "Kaga",
        "Kala/Balge",
        "Konduga",
        "Kukawa",
        "Kwaya Kusar",
        "Mafa",
        "Magumeri",
        "Maiduguri",
        "Marte",
        "Mobbar",
        "Monguno",
        "Ngala",
        "Nganzai",
        "Shani",
    ),
    "CROSS RIVER": (
        "Abi",
        "Akamkpa",
        "Akpabuyo",
        "Bakassi",
        "Bekwarra",
        "Biase",
        "Boki",
        "Calabar Municipal",
        "Calabar South",
        "Etung",
        "Ikom",
        "Obanliku",
        "Obubra",
        "Obudu",
        "Odukpani",
        "Ogoja",
        "Yakuur",
        "Yala",
    ),
    "DELTA": (
        "Aniocha North",
        "Aniocha South",
        "Bomadi",
        "Burutu",
        "Ethiope East",
        "Ethiope West",
        "Ika North-East",
        "Ika South",
        "Isoko North",
        "Isoko South",
        "Ndokwa East",
        "Ndokwa West",
        "Okpe",
        "Oshimili North",
        "Oshimili South",
        "Patani",
        "Sapele",
        "Udu",
        "Ughelli North",
        "Ughelli South",
        "Ukwuani",
        "Uvwie",
        "Warri North",
        "Warri South",
        "Warri South West",
    ),
    "EBONYI": (
        "Abakaliki",
        "Afikpo North",
        "Afikpo South",
        "Ebonyi",
        "Ezza North",
        "Ezza South",
        "Ikwo",
        "Ishielu",
        "Ivo",
        "Izzi",
        "Ohaozara",
        "Ohaukwu",
        "Onicha",
    ),
    "EDO": (
        "Akoko-Edo",
        "Egor",
        "Esan Central",
        "Esan North-East",
        "Esan South-East",
        "Esan West",
        "Etsako Central",
        "Etsako East",
        "Etsako West",
        "Igueben",
        "Ikpoba-Okha",
        "Oredo",
        "Orhionmwon",
        "Ovia North-East",
        "Ovia South-West",
        "Owan East",
        "Owan West",
        "Uhunmwonde",
    ),
    "EKITI": (
        "Ado-Ekiti",
        "Efon",
        "Ekiti East",
        "Ekiti South-West",
        "Ekiti West",
        "Emure",
        "Gbonyin",
        "Ido-Osi",
        "Ijero",
        "Ikere",
        "Ikole",
        "Ilejemeje",
        "Irepodun/Ifelodun",
        "Ise/Orun",
        "Moba",
        "Oye",
    ),
    "ENUGU": (
        "Aninri",
        "Awgu",
        "Enugu East",
        "Enugu North",
        "Enugu South",
        "Ezeagu",
        "Igbo-Etiti",
        "Igbo-Eze North",
        "Igbo-Eze South",
        "Isi-Uzo",
        "Nkanu East",
        "Nkanu West",
        "Nsukka",
        "Oji-River",
        "Udenu",
        "Udi",
        "Uzo-Uwani",
    ),
    "FEDERAL CAPITAL TERRITORY": (
        "Abaji",
        "Bwari",
        "Gwagwalada",
        "Kuje",
        "Kwali",
        "Municipal Area Council",
    ),
    "GOMBE": (
        "Akko",
        "Balanga",
        "Billiri",
        "Dukku",
        "Funakaye",
        "Gombe",
        "Kaltungo",
        "Kwami",
        "Nafada",
        "Shongom",
        "Yamaltu/Deba",
    ),
    "IMO": (
        "Aboh-Mbaise",
        "Ahiazu-Mbaise",
        "Ehime-Mbano",
        "Ezinihitte",
        "Ideato North",
        "Ideato South",
        "Ihitte/Uboma",
        "Ikeduru",
        "Isiala Mbano",
        "Isu",
        "Mbaitoli",
        "Ngor-Okpala",
        "Njaba",
        "Nkwerre",
        "Nwangele",
        "Obowo",
        "Oguta",
        "Ohaji/Egbema",
        "Okigwe",
        "Onuimo",
        "Orlu",
        "Orsu",
        "Oru East",
        "Oru West",
        "Owerri Municipal",
        "Owerri North",
        "Owerri West",
    ),
    "JIGAWA": (
        "Auyo",
        "Babura",
        "Biriniwa",
        "Birnin Kudu",
        "Buji",
        "Dutse",
        "Gagarawa",
        "Garki",
        "Gumel",
        "Guri",
        "Gwaram",
        "Gwiwa",
        "Hadejia",
        "Jahun",
        "Kafin Hausa",
        "Kaugama",
        "Kazaure",
        "Kiri Kasama",
        "Kiyawa",
        "Maigatari",
        "Malam Madori",
        "Miga",
        "Ringim",
        "Roni",
        "Sule-Tankarkar",
        "Taura",
        "Yankwashi",
    ),
    "KADUNA": (
        "Birnin Gwari",
        "Chikun",
        "Giwa",
        "Igabi",
        "Ikara",
        "Jaba",
        "Jema'a",
        "Kachia",
        "Kaduna North",
        "Kaduna South",
        "Kagarko",
        "Kajuru",
        "Kaura",
        "Kauru",
        "Kubau",
        "Kudan",
        "Lere",
        "Makarfi",
        "Sabon Gari",
        "Sanga",
        "Soba",
        "Zangon Kataf",
        "Zaria",
    ),
    "KANO": (
        "Ajingi",
        "Albasu",
        "Bagwai",
        "Bebeji",
        "Bichi",
        "Bunkure",
        "Dala",
        "Dambatta",
        "Dawakin Kudu",
        "Dawakin Tofa",
        "Doguwa",
        "Fagge",
        "Gabasawa",
        "Garko",
        "Garum Mallam",
        "Gaya",
        "Gezawa",
        "Gwale",
        "Gwarzo",
        "Kabo",
        "Kano Municipal",
        "Karaye",
        "Kibiya",
        "Kiru",
        "Kumbotso",
        "Kunchi",
        "Kura",
        "Madobi",
        "Makoda",
        "Minjibir",
        "Nasarawa",
        "Rano",
        "Rimin Gado",
        "Rogo",
        "Shanono",
        "Sumaila",
        "Takai",
        "Tarauni",
        "Tofa",
        "Tsanyawa",
        "Tudun Wada",
        "Ungogo",
        "Warawa",
        "Wudil",
    ),
    "KATSINA": (
        "Bakori",
        "Batagarawa",
        "Batsari",
        "Baure",
        "Bindawa",
        "Charanchi",
        "Dan Musa",
        "Dandume",
        "Danja",
        "Daura",
        "Dutsi",
        "Dutsin-Ma",
        "Faskari",
        "Funtua",
        "Ingawa",
        "Jibia",
        "Kafur",
        "Kaita",
        "Kankara",
        "Kankia",
        "Katsina",
        "Kurfi",
        "Kusada",
        "Mai'adua",
        "Malumfashi",
        "Mani",
        "Mashi",
        "Matazu",
        "Musawa",
        "Rimi",
        "Sabuwa",
        "Safana",
        "Sandamu",
        "Zango",
    ),
    "KEBBI": (
        "Aleiro",
        "Arewa Dandi",
        "Argungu",
        "Augie",
        "Bagudo",
        "Birnin Kebbi",
        "Bunza",
        "Dandi",
        "Fakai",
        "Gwandu",
        "Jega",
        "Kalgo",
        "Koko/Besse",
        "Maiyama",
        "Ngaski",
        "Sakaba",
        "Shanga",
        "Suru",
        "Wasagu/Danko",
        "Yauri",
        "Zuru",
    ),
    "KOGI": (
        "Adavi",
        "Ajaokuta",
        "Ankpa",
        "Bassa",
        "Dekina",
        "Ibaji",
        "Idah",
        "Igalamela-Odolu",
        "Ijumu",
        "Kabba/Bunu",
        "Kogi",
        "Lokoja",
        "Mopa-Muro",
        "Ofu",
        "Ogori/Magongo",
        "Okehi",
        "Okene",
        "Olamaboro",
        "Omala",
        "Yagba East",
        "Yagba West",
    ),
    "KWARA": (
        "Asa",
        "Baruten",
        "Edu",
        "Ekiti",
        "Ifelodun",
        "Ilorin East",
        "Ilorin South",
        "Ilorin West",
        "Irepodun",
        "Isin",
        "Kaiama",
        "Moro",
        "Offa",
        "Oke-Ero",
        "Oyun",
        "Pategi",
    ),
    "LAGOS": (
        "Agege",
        "Ajeromi-Ifelodun",
        "Alimosho",
        "Amuwo-Odofin",
        "Apapa",
        "Badagry",
        "Epe",
        "Eti-Osa",
        "Ibeju-Lekki",
        "Ifako-Ijaiye",
        "Ikeja",
        "Ikorodu",
        "Kosofe",
        "Lagos Island",
        "Lagos Mainland",
        "Mushin",
        "Ojo",
        "Oshodi-Isolo",
        "Shomolu",
        "Surulere",
    ),
    "NASARAWA": (
        "Akwanga",
        "Awe",
        "Doma",
        "Karu",
        "Keana",
        "Keffi",
        "Kokona",
        "Lafia",
        "Nasarawa",
        "Nasarawa Egon",
        "Obi",
        "Toto",
        "Wamba",
    ),
    "NIGER": (
        "Agaie",
        "Agwara",
        "Bida",
        "Borgu",
        "Bosso",
        "Chanchaga",
        "Edati",
        "Gbako",
        "Gurara",
        "Katcha",
        "Kontagora",
        "Lapai",
        "Lavun",
        "Magama",
        "Mariga",
        "Mashegu",
        "Mokwa",
        "Moya",
        "Paikoro",
        "Rafi",
        "Rijau",
        "Shiroro",
        "Suleja",
        "Tafa",
        "Wushishi",
    ),
    "OGUN": (
        "Abeokuta North",
        "Abeokuta South",
        "Ado-Odo/Ota",
        "Egbado North",
        "Egbado South",
        "Ewekoro",
        "Ifo",
        "Ijebu East",
        "Ijebu North",
        "Ijebu North-East",
        "Ijebu Ode",
        "Ikenne",
        "Imeko-Afon",
        "Ipokia",
        "Obafemi-Owode",
        "Odeda",
        "Odogbolu",
        "Ogun Waterside",
        "Remo North",
        "Shagamu",
    ),
    "ONDO": (
        "Akoko North-East",
        "Akoko North-West",
        "Akoko South-East",
        "Akoko South-West",
        "Akure North",
        "Akure South",
        "Ese-Odo",
        "Idanre",
        "Ifedore",
        "Ilaje",
        "Ile-Oluji/Okeigbo",
        "Irele",
        "Odigbo",
        "Okitipupa",
        "Ondo East",
        "Ondo West",
        "Ose",
        "Owo",
    ),
    "OSUN": (
        "Atakumosa East",
        "Atakumosa West",
        "Ayedade",
        "Ayedire",
        "Boluwaduro",
        "Boripe",
        "Ede North",
        "Ede South",
        "Egbedore",
        "Ejigbo",
        "Ife Central",
        "Ife East",
        "Ife North",
        "Ife South",
        "Ifedayo",
        "Ifelodun",
        "Ila",
        "Ilesa East",
        "Ilesa West",
        "Irepodun",
        "Irewole",
        "Isokan",
        "Iwo",
        "Obokun",
        "Odo-Otin",
        "Ola-Oluwa",
        "Olorunda",
        "Oriade",
        "Orolu",
        "Osogbo",
    ),
    "OYO": (
        "Afijio",
        "Akinyele",
        "Atiba",
        "Atisbo",
        "Egbeda",
        "Ibadan North",
        "Ibadan North-East",
        "Ibadan North-West",
        "Ibadan South-East",
        "Ibadan South-West",
        "Ibarapa Central",
        "Ibarapa East",
        "Ibarapa North",
        "Ido",
        "Irepo",
        "Iseyin",
        "Itesiwaju",
        "Iwajowa",
        "Kajola",
        "Lagelu",
        "Ogbomosho North",
        "Ogbomosho South",
        "Ogo Oluwa",
        "Olorunsogo",
        "Oluyole",
        "Ona-Ara",
        "Orelope",
        "Ori-Ire",
        "Oyo East",
        "Oyo West",
        "Saki East",
        "Saki West",
        "Surulere",
    ),
    "PLATEAU": (
        "Barkin Ladi",
        "Bassa",
        "Bokkos",
        "Jos East",
        "Jos North",
        "Jos South",
        "Kanam",
        "Kanke",
        "Langtang North",
        "Langtang South",
        "Mangu",
        "Mikang",
        "Pankshin",
        "Qua'an Pan",
        "Riyom",
        "Shendam",
        "Wase",
    ),
    "RIVERS": (
        "Abua/Odual",
        "Ahoada East",
        "Ahoada West",
        "Akuku-Toru",
        "Andoni",
        "Asari-Toru",
        "Bonny",
        "Degema",
        "Eleme",
        "Emohua",
        "Etche",
        "Gokana",
        "Ikwerre",
        "Khana",
        "Obio/Akpor",
        "Ogba/Egbema/Ndoni",
        "Ogu/Bolo",
        "Okrika",
        "Omuma",
        "Opobo/Nkoro",
        "Oyigbo",
        "Port Harcourt",
        "Tai",
    ),
    "SOKOTO": (
        "Binji",
        "Bodinga",
        "Dange-Shuni",
        "Gada",
        "Goronyo",
        "Gudu",
        "Gwadabawa",
        "Illela",
        "Isa",
        "Kebbe",
        "Kware",
        "Rabah",
        "Sabon Birni",
        "Shagari",
        "Silame",
        "Sokoto North",
        "Sokoto South",
        "Tambuwal",
        "Tangaza",
        "Tureta",
        "Wamako",
        "Wurno",
        "Yabo",
    ),
    "TARABA": (
        "Ardo-Kola",
        "Bali",
        "Donga",
        "Gashaka",
        "Gassol",
        "Ibi",
        "Jalingo",
        "Karim-Lamido",
        "Kumi",
        "Lau",
        "Sardauna",
        "Takum",
        "Ussa",
        "Wukari",
        "Yorro",
        "Zing",
    ),
    "YOBE": (
        "Bade",
        "Bursari",
        "Damaturu",
        "Fika",
        "Fune",
        "Geidam",
        "Gujba",
        "Gulani",
        "Jakusko",
        "Karasuwa",
        "Machina",
        "Nangere",
        "Nguru",
        "Potiskum",
        "Tarmuwa",
        "Yunusari",
        "Yusufari",
    ),
    "ZAMFARA": (
        "Anka",
        "Bakura",
        "Birnin Magaji/Kiyaw",
        "Bukkuyum",
        "Bungudu",
        "Gummi",
        "Gusau",
        "Kaura Namoda",
        "Maradun",
        "Maru",
        "Shinkafi",
        "Talata-Mafara",
        "Tsafe",
        "Zurmi",
    ),
    "INTERNATIONAL": ("International",),
}

_EMPTY_MARKERS = {
    "-",
    "--",
    "---",
    "n/a",
    "na",
    "nil",
    "none",
    "null",
    "unknown",
    "not available",
    "not applicable",
    "not specified",
}


ACCEPTED_CATEGORIES = set(CATEGORY_SLA)
ACCEPTED_CATEGORY_CODES = {str(value["code"]) for value in CATEGORY_SLA.values()}
SUBCATEGORY_BY_CODE = {str(row["issue_code"]): row for row in SUBCATEGORY_ROWS}
ACCEPTED_SUBCATEGORY_CODES = {
    f"{row['issue_code']} - {row['name']}" for row in SUBCATEGORY_ROWS
}
# NCC's own sheets mix hyphen and en-dash separators; accept both, emit hyphen.
_SUBCATEGORY_ALIASES = {
    value.replace(" \u2013 ", " - "): value for value in ACCEPTED_SUBCATEGORY_CODES
}
_SUBCATEGORY_ALIASES.update(
    {value.replace(" - ", " \u2013 "): value for value in ACCEPTED_SUBCATEGORY_CODES}
)


# ── value cleaners (shared with the records builder) ──────────────────────────


def clean_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def clean_basic_text(value: object) -> str:
    """Empty-ish markers ("n/a", "nil", "-", …) collapse to "" so a required
    column carrying a placeholder still fails validation."""
    cleaned = clean_text(value)
    if cleaned.lower() in _EMPTY_MARKERS:
        return ""
    return cleaned


def clean_age(value: object) -> str:
    age_text = clean_text(value)
    if not age_text or not age_text.isdigit():
        return ""
    age = int(age_text)
    return str(age) if 13 <= age <= 150 else ""


def clean_status(value: object) -> str:
    status = clean_basic_text(value)
    return status if status in {"Resolved", "Pending"} else ""


def clean_category(value: object) -> str:
    category = clean_basic_text(value)
    return category if category in ACCEPTED_CATEGORIES else ""


def category_code_value(category: object) -> str:
    cleaned_category = clean_category(category)
    return str(CATEGORY_SLA[cleaned_category]["code"]) if cleaned_category else ""


def clean_subcategory_code(value: object, *, category: object) -> str:
    """A subcategory is only valid under its own category — "A1 - …" filed
    under "Billing" passes, the same code under "SMS / MMS" does not."""
    subcategory = clean_basic_text(value)
    subcategory = _SUBCATEGORY_ALIASES.get(subcategory, subcategory)
    if subcategory not in ACCEPTED_SUBCATEGORY_CODES:
        return ""
    issue_code, _separator, _name = subcategory.partition(" - ")
    row = SUBCATEGORY_BY_CODE.get(issue_code)
    return subcategory if row and row["category"] == clean_category(category) else ""


def name_contains_test(value: object) -> bool:
    return bool(re.search(r"\btest\b", clean_text(value), re.IGNORECASE))


# ── Excel primitives ─────────────────────────────────────────────────────────


def _excel_column_letter(index: int) -> str:
    result = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


_COLUMN_LETTERS = {
    column: _excel_column_letter(index) for index, column in enumerate(COLUMNS, start=1)
}
_TEMPLATE_COLUMN_LETTERS = {
    column: _excel_column_letter(index)
    for index, column in enumerate(TEMPLATE_COLUMNS, start=1)
}

_DATA_ENTRY_FIRST_ROW = 2
_DATA_ENTRY_LAST_ROW = 16001
_DATA_ENTRY_SCAFFOLD_LAST_ROW = 15001
_TEMPLATE_DATE_COLUMNS = {
    "created_date_time *",
    "Resolved_date",
    "user_notes_datetime",
}


def excel_serial_from_display_timestamp(value: str) -> float | None:
    """ "DD/MM/YYYY HH:MM:SS" -> Excel serial. Excel's epoch is 1899-12-30
    (its 1900 leap-year bug baked in)."""
    cleaned = " ".join((value or "").strip().split())
    if not cleaned:
        return None
    try:
        timestamp = datetime.strptime(cleaned, "%d/%m/%Y %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    excel_epoch = datetime(1899, 12, 30, tzinfo=UTC)
    delta = timestamp - excel_epoch
    return delta.days + (delta.seconds / 86400)


def export_filename(value: datetime | None = None) -> str:
    """``<ISO_WEEK>_<ISO_YEAR>_COMPLAINTS_<OPERATOR>.csv`` for Box upload."""
    report_dt = value or datetime.now(UTC)
    if report_dt.tzinfo is None:
        report_dt = report_dt.replace(tzinfo=UTC)
    iso_year, iso_week, _ = report_dt.isocalendar()
    return f"{iso_week:02d}_{iso_year}_COMPLAINTS_{OPERATOR_PREFIX}.csv"


def export_filename_for_window(*, start: datetime, end: datetime) -> str:
    """Name a bounded report by the inclusive reporting end date.

    Accepts inclusive or exclusive ``end`` timestamps. A Monday 00:00 end for a
    Monday-Sunday report still belongs to the previous ISO week.
    """
    start_dt = start if start.tzinfo is not None else start.replace(tzinfo=UTC)
    end_dt = end if end.tzinfo is not None else end.replace(tzinfo=UTC)
    reporting_dt = end_dt - timedelta(microseconds=1) if end_dt > start_dt else end_dt
    return export_filename(reporting_dt)


def regulatory_pack_filename(
    start_dt: datetime, end_dt: datetime, extension: str
) -> str:
    return (
        f"{OPERATOR_NAME}_NCC_Regulatory_Pack_"
        f"{start_dt:%Y%m%d}_{end_dt:%Y%m%d}.{extension}"
    )


def export_rows(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop internal ``_``-prefixed keys (e.g. ``_status_variant``) — they
    drive styling, they are not filed."""
    return [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in records
    ]


def template_export_rows(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """Project internal NCC rows into the official validated-template headers."""
    rows: list[dict[str, str]] = []
    for record in export_rows(records):
        rows.append(
            {
                TEMPLATE_COLUMN_BY_INTERNAL[column]: str(record.get(column, ""))
                for column in COLUMNS
            }
        )
    return rows


def _export_column_widths(
    records: list[dict[str, str]], columns: list[str]
) -> list[float]:
    fixed_widths = {
        "MSISDN": 18,
        "MSISDN *": 22,
        "First Name": 22,
        "First Name *": 18,
        "Last Name": 22,
        "Last Name *": 18,
        "Email": 26,
        "Age": 10,
        "Age *": 8,
        "Gender": 12,
        "Gender *": 12,
        "created date time": 22,
        "created_date_time *": 26,
        "Subject": 28,
        "Category": 24,
        "Category *": 28,
        "category code (auto)": 20,
        "category_code (auto)": 22,
        "sub category code": 22,
        "sub_category_code *": 32,
        "Description (auto)": 55,
        "Ticket ID": 16,
        "Ticket_ID *": 22,
        "Complaint type": 24,
        "Complaint_type *": 18,
        "Status": 18,
        "Status *": 14,
        "Resolved date": 22,
        "Resolved_date": 28,
        "Resolved within SLA": 12,
        "Resolution Note": 45,
        "User Note": 36,
        "User_Note": 40,
        "user notes datetime": 22,
        "user_notes_datetime": 26,
        "Language": 14,
        "Language *": 14,
        "Ticket source": 18,
        "Ticket_source *": 16,
        "alt phone number": 20,
        "alt_phone_number": 22,
        "created by": 24,
        "created_by": 16,
        "State": 14,
        "State *": 22,
        "LGA": 14,
        "LGA *": 26,
        "Town": 18,
        "Phone Type": 28,
        "VALIDATION STATUS": 60,
    }
    widths: list[float] = []
    for column in columns:
        width = fixed_widths.get(column, max(len(column) + 2, 14))
        if column not in fixed_widths:
            max_value_length = max(
                (len(str(row.get(column) or "")) for row in records), default=0
            )
            width = min(max(max_value_length + 2, len(column) + 2, 14), 24)
        widths.append(float(width))
    return widths


def _status_style_id(status_variant: str) -> int:
    mapping = {"success": 5, "warning": 6, "error": 7, "info": 8}
    return mapping.get(status_variant, 9)


# ── validation ───────────────────────────────────────────────────────────────


def validation_status(record: dict[str, str]) -> str:
    """``[OK] All validations passed`` or ``[FAIL] <reason>; <reason>``.

    Every reason names its Excel column letter so the officer can jump
    straight to the offending cell. Rules are NCC's, ported verbatim.
    """
    errors: list[str] = []

    def add_error(column: str, message: str) -> None:
        col_ref = _COLUMN_LETTERS.get(column, "?")
        errors.append(f"{column} {message} (col {col_ref})")

    for column in REQUIRED_COLUMNS:
        value = clean_text(record.get(column))
        # "Unknown" is an accepted answer for Last Name in incomplete CRM data.
        if column == "Last Name" and value == "Unknown":
            continue
        if not value or not clean_basic_text(value):
            add_error(column, "is required")

    msisdn = clean_text(record.get("MSISDN"))
    if msisdn and not msisdn.isdigit():
        add_error("MSISDN", "must contain digits only")
    elif msisdn and not msisdn.startswith("234"):
        add_error("MSISDN", "must start with 234")
    elif msisdn and len(msisdn) != 13:
        add_error("MSISDN", "must be 13 digits including 234")

    first_name = clean_basic_text(record.get("First Name"))
    last_name = clean_basic_text(record.get("Last Name"))
    if first_name and not re.fullmatch(r"[A-Za-z]+", first_name):
        add_error("First Name", "must contain letters only")
    if last_name and not re.fullmatch(r"[A-Za-z-]+", last_name):
        add_error("Last Name", "must contain letters only; hyphen is allowed")
    if name_contains_test(record.get("First Name")):
        add_error("First Name", "must not contain test data")
    if name_contains_test(record.get("Last Name")):
        add_error("Last Name", "must not contain test data")
    if not clean_age(record.get("Age")):
        add_error("Age", "must be a whole number from 13 to 150")
    if clean_text(record.get("Gender")) not in {"Female", "Male"}:
        add_error("Gender", "must be Female or Male")
    if clean_text(record.get("Ticket ID")) and not re.fullmatch(
        rf"{re.escape(OPERATOR_PREFIX)}-\d{{8}}-[A-Za-z0-9-]+",
        clean_text(record.get("Ticket ID")),
    ):
        add_error("Ticket ID", f"must use format {OPERATOR_PREFIX}-YYYYMMDD-Number")
    if clean_text(record.get("Category")) and not clean_category(
        record.get("Category")
    ):
        add_error("Category", "must match an NCC accepted category")
    if clean_text(record.get("sub category code")) and not clean_subcategory_code(
        record.get("sub category code"), category=record.get("Category")
    ):
        add_error("sub category code", "must match the selected NCC category")
    if clean_status(record.get("Status")) == "Resolved":
        if not clean_basic_text(record.get("Resolved date")):
            add_error("Resolved date", "is required when Status is Resolved")
        if not clean_basic_text(record.get("Resolved within SLA")):
            add_error("Resolved within SLA", "is required when Status is Resolved")
        if not clean_basic_text(record.get("Resolution Note")):
            add_error("Resolution Note", "is required when Status is Resolved")
    elif clean_status(record.get("Status")) == "Pending":
        if clean_basic_text(record.get("Resolved date")):
            add_error("Resolved date", "must be empty when Status is Pending")
        if clean_basic_text(record.get("Resolved within SLA")):
            add_error("Resolved within SLA", "must be empty when Status is Pending")
        if clean_basic_text(record.get("Resolution Note")):
            add_error("Resolution Note", "must be empty when Status is Pending")
    if clean_category(
        record.get("Category")
    ) == "Data Depletion" and not clean_basic_text(record.get("Phone Type")):
        add_error("Phone Type", "is required when Category is Data Depletion")
    return f"[FAIL] {'; '.join(errors)}" if errors else "[OK] All validations passed"


# ── workbook ─────────────────────────────────────────────────────────────────

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_APP_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Dotmac Sub</Application>
</Properties>"""

_WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_LOOKUPS_CATEGORY_COLUMN = 38  # AL
_LOOKUPS_CATEGORY_CODE_COLUMN = 39  # AM
_LOOKUPS_STATE_COLUMN = 40  # AN
_LOOKUPS_CATEGORY_LIST_START_COLUMN = 41  # AO
_LOOKUPS_SUBCATEGORY_CODE_COLUMN = 60  # BH
_LOOKUPS_SUBCATEGORY_DESCRIPTION_COLUMN = 61  # BI
_LOOKUPS_INTERNATIONAL_COLUMN = 62  # BJ


def _excel_defined_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]", "_", value.strip())


def _subcategory_options_by_category() -> dict[str, list[str]]:
    options: dict[str, list[str]] = {category: [] for category in CATEGORY_SLA}
    for row in SUBCATEGORY_ROWS:
        category = str(row["category"])
        options.setdefault(category, []).append(f"{row['issue_code']} - {row['name']}")
    return options


def _defined_names_xml() -> str:
    state_lga_states = [state for state in STATE_LGAS if state != "INTERNATIONAL"]
    category_options = _subcategory_options_by_category()
    defined_names: list[str] = []

    for column_index, state in enumerate(state_lga_states, start=1):
        values = STATE_LGAS[state]
        if not values:
            continue
        column_letter = _excel_column_letter(column_index)
        name = _excel_defined_name(state.replace(" ", "_"))
        defined_names.append(
            f'<definedName name="{escape(name)}">'
            f"Lookups!${column_letter}$2:${column_letter}${len(values) + 1}"
            "</definedName>"
        )

    category_letter = _excel_column_letter(_LOOKUPS_CATEGORY_COLUMN)
    category_code_letter = _excel_column_letter(_LOOKUPS_CATEGORY_CODE_COLUMN)
    state_letter = _excel_column_letter(_LOOKUPS_STATE_COLUMN)
    subcategory_code_letter = _excel_column_letter(_LOOKUPS_SUBCATEGORY_CODE_COLUMN)
    subcategory_description_letter = _excel_column_letter(
        _LOOKUPS_SUBCATEGORY_DESCRIPTION_COLUMN
    )
    international_letter = _excel_column_letter(_LOOKUPS_INTERNATIONAL_COLUMN)
    defined_names.extend(
        (
            f'<definedName name="NCC_CATEGORIES">'
            f"Lookups!${category_letter}$2:${category_letter}${len(CATEGORY_SLA) + 1}"
            "</definedName>",
            f'<definedName name="NCC_CAT_CODES">'
            f"Lookups!${category_code_letter}$2:${category_code_letter}"
            f"{len(CATEGORY_SLA) + 1}</definedName>",
            f'<definedName name="NCC_STATES">'
            f"Lookups!${state_letter}$2:${state_letter}${len(STATE_LGAS) + 1}"
            "</definedName>",
        )
    )

    for offset, (category, config) in enumerate(CATEGORY_SLA.items()):
        options = category_options.get(category, [])
        if not options:
            continue
        column_letter = _excel_column_letter(
            _LOOKUPS_CATEGORY_LIST_START_COLUMN + offset
        )
        name = f"CAT_{config['code']}"
        defined_names.append(
            f'<definedName name="{escape(str(name))}">'
            f"Lookups!${column_letter}$2:${column_letter}${len(options) + 1}"
            "</definedName>"
        )

    defined_names.extend(
        (
            f'<definedName name="SUBCAT_DESC">'
            f"Lookups!${subcategory_code_letter}$2:"
            f"${subcategory_description_letter}${len(SUBCATEGORY_ROWS) + 1}"
            "</definedName>",
            f'<definedName name="INTERNATIONAL">'
            f"Lookups!${international_letter}$2:${international_letter}$2"
            "</definedName>",
        )
    )
    return f"<definedNames>{''.join(defined_names)}</definedNames>"


def _workbook_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView firstSheet="1" activeTab="1"/></bookViews>
  <sheets>
    <sheet name="Lookups" sheetId="1" state="hidden" r:id="rId1"/>
    <sheet name="Data Entry" sheetId="2" r:id="rId2"/>
  </sheets>
  {_defined_names_xml()}
</workbook>"""


# Style ids consumed below: 1 header, 2 cell, 3 wrapped cell, 4 date,
# 5-9 status variants (_status_style_id), 10 row OK (green), 11 row FAIL (red).
_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1">
    <numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/>
  </numFmts>
  <fonts count="2">
    <font>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <b/>
      <sz val="11"/>
      <color rgb="FFFFFFFF"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
  </fonts>
  <fills count="7">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF16A34A"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDCFCE7"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFEF3C7"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFEE2E2"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDBEAFE"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border>
      <left/><right/><top/><bottom/><diagonal/>
    </border>
    <border>
      <left style="thin"><color rgb="FFD1D5DB"/></left>
      <right style="thin"><color rgb="FFD1D5DB"/></right>
      <top style="thin"><color rgb="FFD1D5DB"/></top>
      <bottom style="thin"><color rgb="FFD1D5DB"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="12">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>"""

_LONG_TEXT_COLUMNS = {
    "Description (auto)",
    "Resolution Note",
    "User Note",
    "User_Note",
    "VALIDATION STATUS",
}


def _cell_xml(ref: str, value: str, style_id: int) -> str:
    return (
        f'<c r="{ref}" s="{style_id}" t="inlineStr"><is><t xml:space="preserve">'
        f"{escape(str(value or ''))}</t></is></c>"
    )


def _numeric_cell_xml(ref: str, value: float, style_id: int) -> str:
    return f'<c r="{ref}" s="{style_id}"><v>{value:.12g}</v></c>'


def _formula_string_cell_xml(
    ref: str, formula: str, cached_value: str, style_id: int
) -> str:
    return (
        f'<c r="{ref}" s="{style_id}" t="str"><f>{escape(formula)}</f>'
        f"<v>{escape(str(cached_value or ''))}</v></c>"
    )


def _template_auto_formula(column: str, row_number: int) -> str | None:
    if column == "category_code (auto)":
        return (
            f'IF($I{row_number}="","",'
            f'IFERROR(VLOOKUP($I{row_number},Lookups!$AL$2:$AM$19,2,0),""))'
        )
    if column == "Description (auto)":
        return (
            f'IF($K{row_number}="","",'
            f'IFERROR(VLOOKUP($K{row_number},Lookups!$BH$2:$BI$88,2,0),""))'
        )
    return None


def _lookups_sheet_xml() -> str:
    """The hidden NCC lookup grid used by the validated template."""
    state_lga_states = [state for state in STATE_LGAS if state != "INTERNATIONAL"]
    category_options = _subcategory_options_by_category()
    cells_by_row: dict[int, list[str]] = {}

    def add_cell(
        row_number: int, column_index: int, value: object, style_id: int
    ) -> None:
        cells_by_row.setdefault(row_number, []).append(
            _cell_xml(
                f"{_excel_column_letter(column_index)}{row_number}",
                str(value),
                style_id,
            )
        )

    for column_index, state in enumerate(state_lga_states, start=1):
        add_cell(1, column_index, _excel_defined_name(state.replace(" ", "_")), 1)
        for row_index, lga in enumerate(STATE_LGAS[state], start=2):
            add_cell(row_index, column_index, lga, 2)

    add_cell(1, _LOOKUPS_CATEGORY_COLUMN, "NCC_CATEGORIES", 1)
    add_cell(1, _LOOKUPS_CATEGORY_CODE_COLUMN, "NCC_CAT_CODES", 1)
    add_cell(1, _LOOKUPS_STATE_COLUMN, "NCC_STATES", 1)
    for row_index, (category, config) in enumerate(CATEGORY_SLA.items(), start=2):
        add_cell(row_index, _LOOKUPS_CATEGORY_COLUMN, category, 2)
        add_cell(row_index, _LOOKUPS_CATEGORY_CODE_COLUMN, config["code"], 2)
    for row_index, state in enumerate(STATE_LGAS, start=2):
        add_cell(row_index, _LOOKUPS_STATE_COLUMN, state, 2)

    for offset, (category, config) in enumerate(CATEGORY_SLA.items()):
        column_index = _LOOKUPS_CATEGORY_LIST_START_COLUMN + offset
        add_cell(1, column_index, f"CAT_{config['code']}", 1)
        for row_index, option in enumerate(category_options.get(category, []), start=2):
            add_cell(row_index, column_index, option, 2)

    add_cell(1, _LOOKUPS_SUBCATEGORY_CODE_COLUMN, "sub_category_code", 1)
    add_cell(1, _LOOKUPS_SUBCATEGORY_DESCRIPTION_COLUMN, "Description", 1)
    for row_index, row in enumerate(SUBCATEGORY_ROWS, start=2):
        add_cell(row_index, _LOOKUPS_SUBCATEGORY_CODE_COLUMN, row["issue_code"], 2)
        add_cell(
            row_index,
            _LOOKUPS_SUBCATEGORY_DESCRIPTION_COLUMN,
            row["description"],
            2,
        )

    add_cell(1, _LOOKUPS_INTERNATIONAL_COLUMN, "INTERNATIONAL", 1)
    for row_index, lga in enumerate(STATE_LGAS.get("INTERNATIONAL", ()), start=2):
        add_cell(row_index, _LOOKUPS_INTERNATIONAL_COLUMN, lga, 2)

    rows = [
        f'<row r="{row_number}">{"".join(cells)}</row>'
        for row_number, cells in sorted(cells_by_row.items())
    ]
    last_column = _excel_column_letter(_LOOKUPS_INTERNATIONAL_COLUMN)
    last_row = max(cells_by_row, default=1)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:{last_column}{last_row}"/>
  <sheetViews><sheetView workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <sheetData>{"".join(rows)}</sheetData>
</worksheet>"""


def _data_validation_xml(
    *,
    validation_type: str | None,
    sqref: str,
    formula1: str | None,
    allow_blank: bool,
    error_title: str | None = None,
    error: str | None = None,
    operator: str | None = None,
    prompt_title: str | None = None,
    prompt: str | None = None,
    formula2: str | None = "0",
) -> str:
    type_xml = f' type="{validation_type}"' if validation_type else ""
    allow_blank_xml = ' allowBlank="1"' if allow_blank else ""
    error_xml = ' showErrorMessage="1"' if error_title or error else ""
    if error_title:
        error_xml += f' errorTitle="{escape(error_title)}"'
    if error:
        error_xml += f' error="{escape(error)}"'
    operator_xml = f' operator="{operator}"' if operator else ""
    prompt_xml = ""
    if prompt_title or prompt:
        prompt_xml = (
            f' showInputMessage="1" promptTitle="{escape(prompt_title or "")}"'
            f' prompt="{escape(prompt or "")}"'
        )
    formulas_xml = ""
    if formula1 is not None:
        formulas_xml += f"<formula1>{escape(formula1)}</formula1>"
    if formula2 is not None:
        formulas_xml += f"<formula2>{escape(formula2)}</formula2>"
    return (
        f"<dataValidation{type_xml}{allow_blank_xml}{error_xml}{prompt_xml}"
        f'{operator_xml} sqref="{sqref}">{formulas_xml}</dataValidation>'
    )


def _list_validation_xml(
    *,
    column: str,
    formula1: str,
    allow_blank: bool,
    error_title: str | None = None,
) -> str:
    letter = _TEMPLATE_COLUMN_LETTERS[column]
    return _data_validation_xml(
        validation_type="list",
        allow_blank=allow_blank,
        error_title=error_title,
        sqref=f"{letter}{_DATA_ENTRY_FIRST_ROW}:{letter}{_DATA_ENTRY_LAST_ROW}",
        formula1=formula1,
    )


def _data_validations_xml(columns: list[str]) -> str:
    if columns != TEMPLATE_COLUMNS:
        return ""

    row = _DATA_ENTRY_FIRST_ROW
    validations = [
        _data_validation_xml(
            validation_type="textLength",
            allow_blank=False,
            operator="greaterThanOrEqual",
            sqref=(
                f"B{_DATA_ENTRY_FIRST_ROW}:C{_DATA_ENTRY_LAST_ROW}"
                f" M{_DATA_ENTRY_FIRST_ROW}:M{_DATA_ENTRY_LAST_ROW}"
            ),
            formula1="1",
        ),
        _data_validation_xml(
            validation_type="custom",
            allow_blank=True,
            sqref=f"D{_DATA_ENTRY_FIRST_ROW}:D{_DATA_ENTRY_LAST_ROW}",
            formula1=(
                f'OR(D{row}="",AND(ISNUMBER(FIND("@",D{row})),'
                f'ISNUMBER(FIND(".",D{row},FIND("@",D{row})+1))))'
            ),
        ),
        _data_validation_xml(
            validation_type="whole",
            allow_blank=False,
            sqref=f"E{_DATA_ENTRY_FIRST_ROW}:E{_DATA_ENTRY_LAST_ROW}",
            formula1="13",
            formula2="150",
        ),
        _list_validation_xml(
            column="Gender *", formula1='"Male,Female"', allow_blank=False
        ),
        _data_validation_xml(
            validation_type="custom",
            allow_blank=False,
            sqref=f"G{_DATA_ENTRY_FIRST_ROW}:G{_DATA_ENTRY_LAST_ROW}",
            formula1=(
                f"AND(G{row}>=DATE(2000,1,1),G{row}<=DATE(2099,12,31),MOD(G{row},1)>0)"
            ),
        ),
        _list_validation_xml(
            column="Category *", formula1="NCC_CATEGORIES", allow_blank=False
        ),
        _list_validation_xml(
            column="sub_category_code *",
            formula1=f'INDIRECT("CAT_"&$J{row})',
            allow_blank=False,
        ),
        _list_validation_xml(
            column="Complaint_type *",
            formula1='"First Level,Second Level"',
            allow_blank=False,
        ),
        _list_validation_xml(
            column="Status *", formula1='"Resolved,Pending"', allow_blank=False
        ),
        _data_validation_xml(
            validation_type="custom",
            allow_blank=True,
            sqref=(
                f"P{_DATA_ENTRY_FIRST_ROW}:P{_DATA_ENTRY_LAST_ROW} "
                f"T{_DATA_ENTRY_FIRST_ROW}:T{_DATA_ENTRY_LAST_ROW}"
            ),
            formula1=(
                f"AND(P{row}>=DATE(2000,1,1),P{row}<=DATE(2099,12,31),MOD(P{row},1)>0)"
            ),
        ),
        _list_validation_xml(
            column="Resolved within SLA", formula1='"Yes,No"', allow_blank=True
        ),
        _list_validation_xml(
            column="Language *",
            formula1='"English,Hausa,Igbo,Yoruba,Pidgin,Others"',
            allow_blank=False,
        ),
        _list_validation_xml(
            column="Ticket_source *",
            formula1='"Phone Call,Email,Web Portal,Mobile App,Walk-in,SMS,Social Media,Other"',
            allow_blank=False,
        ),
        _data_validation_xml(
            validation_type="custom",
            allow_blank=True,
            sqref=f"W{_DATA_ENTRY_FIRST_ROW}:W{_DATA_ENTRY_LAST_ROW}",
            formula1=(
                f'OR(W{row}="",AND(ISNUMBER(VALUE(W{row})),'
                f'LEN(TRIM(TEXT(W{row},"0")))>=10,'
                f'LEN(TRIM(TEXT(W{row},"0")))<=15))'
            ),
        ),
        _list_validation_xml(
            column="State *", formula1="NCC_STATES", allow_blank=False
        ),
        _list_validation_xml(
            column="LGA *",
            formula1=f'INDIRECT(SUBSTITUTE($Y{row}," ","_"))',
            allow_blank=False,
        ),
        _data_validation_xml(
            validation_type=None,
            allow_blank=True,
            sqref=f"AB{_DATA_ENTRY_FIRST_ROW}:AB{_DATA_ENTRY_LAST_ROW}",
            formula1="0",
            formula2="0",
            prompt_title="Phone Type",
            prompt=(
                "Required for Data Depletion complaints. Enter the complainant's "
                "device type."
            ),
        ),
    ]
    return (
        f'<dataValidations count="{len(validations)}">'
        f"{''.join(validations)}</dataValidations>"
    )


def _row_value(row: dict[str, str], column: str) -> str:
    if column in row:
        return row[column]
    internal_column = INTERNAL_COLUMN_BY_TEMPLATE.get(column)
    if internal_column:
        return row.get(internal_column, "")
    template_column = TEMPLATE_COLUMN_BY_INTERNAL.get(column)
    if template_column:
        return row.get(template_column, "")
    return ""


def build_csv(records: list[dict[str, str]], columns: list[str]) -> bytes:
    """Build the single provider CSV filed to NCC through Box."""
    output_columns = [
        TEMPLATE_COLUMN_BY_INTERNAL.get(column, column) for column in columns
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=output_columns,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for row in records:
        writer.writerow(
            {
                column: " ".join(str(_row_value(row, column) or "").strip().split())
                for column in output_columns
            }
        )
    return output.getvalue().encode("utf-8")


def build_workbook(records: list[dict[str, str]], columns: list[str]) -> bytes:
    """Validation workbook: the official Data Entry sheet plus hidden Lookups.

    Rows shade green/red from their VALIDATION STATUS so the officer can see
    at a glance what needs fixing before submission.
    """
    output_columns = [
        TEMPLATE_COLUMN_BY_INTERNAL.get(column, column) for column in columns
    ]
    widths = _export_column_widths(records, output_columns)
    output = io.BytesIO()

    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", _ROOT_RELS_XML)
        generated_at = (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        archive.writestr(
            "docProps/core.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(EXPORT_TITLE)}</dc:title>
  <dc:creator>Dotmac Sub</dc:creator>
  <cp:lastModifiedBy>Dotmac Sub</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{generated_at}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{generated_at}</dcterms:modified>
</cp:coreProperties>""",
        )
        archive.writestr("docProps/app.xml", _APP_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
        archive.writestr("xl/workbook.xml", _workbook_xml())
        archive.writestr("xl/styles.xml", _STYLES_XML)

        last_column_letter = _excel_column_letter(len(output_columns))
        data_last_row_number = max(1, _DATA_ENTRY_FIRST_ROW + len(records) - 1)
        template_layout = output_columns == TEMPLATE_COLUMNS
        last_row_number = (
            max(_DATA_ENTRY_SCAFFOLD_LAST_ROW, data_last_row_number)
            if template_layout
            else data_last_row_number
        )
        cols_xml = "".join(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(widths, start=1)
        )
        rows_xml: list[str] = []
        header_cells = [
            _cell_xml(f"{_excel_column_letter(index)}1", column, 1)
            for index, column in enumerate(output_columns, start=1)
        ]
        rows_xml.append(
            f'<row r="1" ht="64.5" customHeight="1">{"".join(header_cells)}</row>'
        )
        for row_number, row in enumerate(records, start=_DATA_ENTRY_FIRST_ROW):
            cells: list[str] = []
            row_validation = clean_text(_row_value(row, "VALIDATION STATUS"))
            row_style_id = (
                10
                if row_validation.startswith("[OK]")
                else 11
                if row_validation.startswith("[FAIL]")
                else None
            )
            for column_index, column in enumerate(output_columns, start=1):
                value = " ".join(str(_row_value(row, column) or "").strip().split())
                auto_formula = _template_auto_formula(column, row_number)
                if not value and not auto_formula:
                    continue
                cell_ref = f"{_excel_column_letter(column_index)}{row_number}"
                if column in _TEMPLATE_DATE_COLUMNS:
                    serial_value = excel_serial_from_display_timestamp(value)
                    if serial_value is not None:
                        cells.append(_numeric_cell_xml(cell_ref, serial_value, 4))
                        continue
                if auto_formula is not None:
                    cells.append(
                        _formula_string_cell_xml(cell_ref, auto_formula, value, 9)
                    )
                    continue
                if row_style_id is not None:
                    style_id = row_style_id
                elif column in {"Status", "Status *"}:
                    style_id = _status_style_id(str(row.get("_status_variant") or ""))
                elif column in _LONG_TEXT_COLUMNS:
                    style_id = 3
                else:
                    style_id = 2
                cells.append(_cell_xml(cell_ref, value, style_id))
            rows_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')

        archive.writestr(
            "xl/worksheets/sheet2.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:{last_column_letter}{last_row_number}"/>
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft" activeCell="D3" sqref="D3"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <cols>{cols_xml}</cols>
  <sheetData>{"".join(rows_xml)}</sheetData>
  {_data_validations_xml(output_columns)}
</worksheet>""",
        )
        archive.writestr("xl/worksheets/sheet1.xml", _lookups_sheet_xml())

    return output.getvalue()
