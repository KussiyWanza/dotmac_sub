"""Huawei response semantics must remain owned by one classifier."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TARGETS = {
    ROOT / "app/services/network/parsers/cli.py",
    ROOT / "app/services/network/olt_ssh_session.py",
    ROOT / "app/services/network/olt_protocol_adapters.py",
    ROOT / "app/services/network/ont_inventory.py",
    ROOT / "app/services/network/ont_authorization.py",
    ROOT / "app/services/network/olt_config_pack_live_audit.py",
    ROOT / "app/services/web_network_ont_actions/config_setters.py",
}
TARGETS.update((ROOT / "app/services/network").glob("olt_ssh*.py"))
TARGETS.update((ROOT / "app/services/network/olt_ssh_ont").glob("*.py"))

RESPONSE_MARKERS = (
    "already exists",
    "does not exist",
    "insufficient privilege",
    "is not exist",
    "ont is not online",
    "parameter error",
    "unknown command",
)


def _local_response_comparisons(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        values = [node.left, *node.comparators]
        for value in values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            lowered = value.value.casefold()
            if any(marker in lowered for marker in RESPONSE_MARKERS):
                offenders.append((node.lineno, value.value))
    return offenders


def test_huawei_cli_response_text_has_one_owner() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): matches
        for path in sorted(TARGETS)
        if (matches := _local_response_comparisons(path))
    }
    assert not offenders, (
        "Huawei CLI response text must be classified by "
        "app.services.network.huawei_cli_response; local string comparisons "
        f"create firmware-dependent drift: {offenders}"
    )


_WRAPPER_RE = re.compile(r"^\s*olt\s+(?:rejected|error)\b", re.IGNORECASE)


def _logger_call_strings(tree: ast.AST) -> set[int]:
    """Node ids of string constants used as logging format arguments."""
    logged: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        target = func.value
        if not (isinstance(target, ast.Name) and target.id in {"logger", "logging"}):
            continue
        for arg in node.args:
            logged.add(id(arg))
    return logged


def _hand_rolled_rejection_wrappers(path: Path) -> list[tuple[int, str]]:
    """Rejection prefixes built locally instead of by the owner."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    logged = _logger_call_strings(tree)
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in logged:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _WRAPPER_RE.match(node.value):
                offenders.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and _WRAPPER_RE.match(head.value)
            ):
                offenders.append((node.lineno, head.value))
    return offenders


def test_rejection_wrappers_have_one_owner() -> None:
    """Operator-facing rejection text must come from the classifier module.

    Each call site used to invent its own prefix. The classifier could parse
    only the bare ``OLT rejected:`` form, so ``authorize_ont``'s
    ``OLT rejected command:`` wrapper made a genuine duplicate-serial rejection
    classify as "no error", which silently disabled the reuse/move recovery
    branch in ``ont_authorization``.
    """
    offenders = {
        path.relative_to(ROOT).as_posix(): matches
        for path in sorted(TARGETS)
        if (matches := _hand_rolled_rejection_wrappers(path))
    }
    assert not offenders, (
        "Build rejection messages with "
        "app.services.network.huawei_cli_response.describe_huawei_rejection; "
        f"local prefixes drift out of the classifier's envelope: {offenders}"
    )


#: Every module that turns an F/S/P into a device command or a match pattern.
_FSP_CONSUMERS = sorted(
    {
        *(ROOT / "app/services/network/olt_ssh_ont").glob("*.py"),
        *(ROOT / "app/services/network").glob("olt_ssh*.py"),
        ROOT / "app/services/network/olt_command_gen.py",
        ROOT / "app/services/network/olt_vendor_adapters.py",
        ROOT / "app/services/network/olt_batched_mgmt.py",
        ROOT / "app/services/network/olt_diagnostics.py",
        ROOT / "app/services/network/olt_profile_resolution.py",
        ROOT / "app/services/network/huawei_command_profiles.py",
        ROOT / "app/services/network/ont_write.py",
    }
)


def _raw_fsp_splits(path: Path) -> list[int]:
    """Lines that slice a Frame/Slot/Port string by hand."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "split"):
            continue
        target = func.value
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if name.endswith("fsp"):
            offenders.append(node.lineno)
    return offenders


def test_olt_commands_are_built_from_canonical_fsp() -> None:
    """F/S/P must be canonicalized before a command is built from it.

    The boolean ``validate_fsp`` normalizes port-name prefixes
    (``gpon-0/1/0``) before matching but returns only a verdict. Callers that
    then split the *raw* value accepted the port and emitted
    ``interface gpon gpon-0/1`` / ``service-port ... gpon gpon-0/1/0 ...``.
    """
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for path in _FSP_CONSUMERS
        if (lines := _raw_fsp_splits(path))
    }
    assert not offenders, (
        "Use app.services.network.parsers.cli.canonical_fsp and build commands "
        f"from FspParts.frame_slot / .port: {offenders}"
    )


def test_legacy_entry_points_delegate_to_classifier() -> None:
    parser_source = (ROOT / "app/services/network/parsers/cli.py").read_text(
        encoding="utf-8"
    )
    session_source = (ROOT / "app/services/network/olt_ssh_session.py").read_text(
        encoding="utf-8"
    )

    assert "has_huawei_cli_error" in parser_source
    assert "classify_huawei_cli_response" in session_source
    assert "HUAWEI_ERROR_PATTERNS" not in parser_source
    assert "_ERROR_PATTERNS" not in session_source
