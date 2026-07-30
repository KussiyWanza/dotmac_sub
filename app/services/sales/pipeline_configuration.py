"""Governed presentation vocabulary for native sales pipeline stages.

Pipeline and stage lifecycle remains owned by :mod:`app.services.sales.service`.
This module defines the typed metadata contract used to identify and render
stages consistently without turning the model's metadata column into an
arbitrary settings bag.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

METADATA_KEY = "pipeline_stage_presentation_v1"
DEFAULT_STAGE_COLOR = "#06B6D4"
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class PipelineStageType(StrEnum):
    standard = "standard"
    closed_won = "closed_won"
    closed_lost = "closed_lost"


STAGE_ICON_OPTIONS: tuple[tuple[str, str], ...] = (
    ("", "No icon"),
    ("circle", "Circle"),
    ("target", "Target"),
    ("document", "Document"),
    ("handshake", "Handshake"),
    ("clock", "Clock"),
    ("check", "Check"),
    ("close", "Close"),
)
_STAGE_ICON_KEYS = frozenset(key for key, _label in STAGE_ICON_OPTIONS)


@dataclass(frozen=True)
class PipelineStagePresentation:
    stage_type: PipelineStageType
    color: str
    icon: str | None


def _inferred_stage_type(name: str) -> PipelineStageType:
    normalized = " ".join(str(name or "").strip().lower().replace("_", " ").split())
    if normalized in {"closed won", "won"}:
        return PipelineStageType.closed_won
    if normalized in {"closed lost", "lost"}:
        return PipelineStageType.closed_lost
    return PipelineStageType.standard


def normalize_stage_type(
    value: object | None,
    *,
    stage_name: str,
) -> PipelineStageType:
    try:
        return PipelineStageType(str(value or ""))
    except ValueError:
        return _inferred_stage_type(stage_name)


def validate_stage_type(value: object | None) -> PipelineStageType:
    try:
        return PipelineStageType(str(value or ""))
    except ValueError as exc:
        raise ValueError("Unsupported pipeline stage type.") from exc


def normalize_stage_color(value: object | None) -> str:
    color = str(value or DEFAULT_STAGE_COLOR).strip().upper()
    if not _HEX_COLOR.fullmatch(color):
        raise ValueError("Stage color must be a six-digit hexadecimal color.")
    return color


def normalize_stage_icon(value: object | None) -> str | None:
    icon = str(value or "").strip().lower()
    if icon not in _STAGE_ICON_KEYS:
        raise ValueError("Unsupported pipeline stage icon.")
    return icon or None


def stage_presentation(
    *,
    stage_name: str,
    metadata: Mapping[str, object] | None,
) -> PipelineStagePresentation:
    raw = (metadata or {}).get(METADATA_KEY)
    config = raw if isinstance(raw, Mapping) else {}
    try:
        color = normalize_stage_color(config.get("color"))
    except ValueError:
        color = DEFAULT_STAGE_COLOR
    try:
        icon = normalize_stage_icon(config.get("icon"))
    except ValueError:
        icon = None
    return PipelineStagePresentation(
        stage_type=normalize_stage_type(
            config.get("stage_type"),
            stage_name=stage_name,
        ),
        color=color,
        icon=icon,
    )


def metadata_with_stage_presentation(
    metadata: Mapping[str, object] | None,
    *,
    stage_name: str,
    stage_type: object | None,
    color: object | None,
    icon: object | None,
) -> dict[str, object]:
    """Return metadata with exactly one validated presentation contract."""

    resolved_type = validate_stage_type(stage_type)
    resolved_color = normalize_stage_color(color)
    resolved_icon = normalize_stage_icon(icon)
    result = dict(metadata or {})
    result[METADATA_KEY] = {
        "stage_type": resolved_type.value,
        "color": resolved_color,
        "icon": resolved_icon,
    }
    return result
