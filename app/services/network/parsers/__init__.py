"""OLT CLI output parsers using TextFSM templates."""

from app.services.network.parsers.cli import (
    HUAWEI_OPTIONAL_ARG_PROMPT,
    FspParts,
    canonical_fsp,
    is_error_output,
    needs_huawei_command_confirm,
    normalize_fsp,
    validate_fsp,
    validate_readonly_command,
    validate_serial,
)
from app.services.network.parsers.firmware import FirmwareInfo, parse_firmware_info
from app.services.network.parsers.loader import (
    ParseError,
    ParseResult,
    parse_autofind,
    parse_key_value,
    parse_ont_info,
    parse_ont_info_detail,
    parse_profile_table,
)
from app.services.network.parsers.service_ports import (
    ServicePortEntry,
    parse_service_port_table,
    parse_service_port_table_legacy,
)

__all__ = [
    "FirmwareInfo",
    "FspParts",
    "HUAWEI_OPTIONAL_ARG_PROMPT",
    "ParseError",
    "ParseResult",
    "ServicePortEntry",
    "canonical_fsp",
    "is_error_output",
    "needs_huawei_command_confirm",
    "normalize_fsp",
    "parse_autofind",
    "parse_firmware_info",
    "parse_key_value",
    "parse_ont_info",
    "parse_ont_info_detail",
    "parse_profile_table",
    "parse_service_port_table",
    "parse_service_port_table_legacy",
    "validate_fsp",
    "validate_readonly_command",
    "validate_serial",
]
