"""Conservatively classify whether a change needs PostgreSQL validation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class PostgreSQLValidationDecision:
    required: bool
    requiring_paths: tuple[str, ...]


def _is_database_independent(path: PurePosixPath) -> bool:
    value = path.as_posix()
    if value.endswith(".md"):
        return True
    if path.parts and path.parts[0] in {
        "docs",
        "templates",
        "static",
        "mobile",
    }:
        return True
    if path.parts and path.parts[0] == "tests":
        return not (
            value == "tests/conftest.py" or value.startswith("tests/integration/")
        )
    return False


def classify_postgresql_changes(
    changed_paths: tuple[str, ...],
) -> PostgreSQLValidationDecision:
    """Fail closed for empty or backend-affecting change sets."""

    normalized = tuple(
        PurePosixPath(path.strip()).as_posix() for path in changed_paths if path.strip()
    )
    if not normalized:
        return PostgreSQLValidationDecision(required=True, requiring_paths=())
    requiring_paths = tuple(
        path for path in normalized if not _is_database_independent(PurePosixPath(path))
    )
    return PostgreSQLValidationDecision(
        required=bool(requiring_paths),
        requiring_paths=requiring_paths,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths-file", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    decision = classify_postgresql_changes(
        tuple(args.paths_file.read_text(encoding="utf-8").splitlines())
    )
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"postgresql-required={str(decision.required).lower()}\n")


if __name__ == "__main__":
    main()
