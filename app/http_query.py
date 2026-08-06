"""Typed normalization for HTTP query parameters."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import Query
from pydantic import BeforeValidator


def _empty_string_as_none(value: object) -> object:
    """Treat an unselected HTML input as an omitted optional value."""

    return None if value == "" else value


OptionalDateQuery = Annotated[
    date | None,
    Query(),
    BeforeValidator(_empty_string_as_none),
]
