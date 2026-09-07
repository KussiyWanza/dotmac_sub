"""Application version helpers."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_app_version() -> str:
    """Return the deployed application version."""
    env_version = os.getenv("APP_VERSION")
    if env_version:
        return env_version.strip()

    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return version or "unknown"


@lru_cache(maxsize=1)
def get_app_revision() -> str | None:
    """Return the immutable deployed source revision when the host provides it."""

    revision = (
        os.getenv("APP_REVISION") or os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA")
    )
    return revision.strip() if revision and revision.strip() else None
