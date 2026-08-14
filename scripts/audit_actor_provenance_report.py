#!/usr/bin/env python3
"""Inventory typed and legacy audit-actor callers without reading row data."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

AUDIT_FACADES = frozenset({"record_audit_event", "stage_audit_event"})
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = (
    DEFAULT_PROJECT_ROOT
    / "tests"
    / "architecture"
    / "audit_actor_provenance_baseline.json"
)


class AuditActorCallKind(StrEnum):
    TYPED = "typed"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class AuditActorCallSite:
    path: str
    scope: str
    facade: str
    ordinal: int
    kind: AuditActorCallKind

    @property
    def stable_id(self) -> str:
        return f"{self.path}|{self.scope}|{self.facade}|{self.ordinal}"


@dataclass(frozen=True, slots=True)
class AuditActorProvenanceReport:
    sites: tuple[AuditActorCallSite, ...]

    @property
    def legacy_sites(self) -> tuple[AuditActorCallSite, ...]:
        return tuple(
            site for site in self.sites if site.kind is AuditActorCallKind.LEGACY
        )

    @property
    def typed_sites(self) -> tuple[AuditActorCallSite, ...]:
        return tuple(
            site for site in self.sites if site.kind is AuditActorCallKind.TYPED
        )

    @property
    def legacy_call_count(self) -> int:
        return len(self.legacy_sites)

    @property
    def typed_call_count(self) -> int:
        return len(self.typed_sites)

    @property
    def module_count(self) -> int:
        return len({site.path for site in self.sites})

    @property
    def legacy_sites_sha256(self) -> str:
        evidence = "\n".join(site.stable_id for site in self.legacy_sites)
        return hashlib.sha256(evidence.encode("utf-8")).hexdigest()

    def as_dict(self, *, include_sites: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "module_count": self.module_count,
            "typed_call_count": self.typed_call_count,
            "legacy_call_count": self.legacy_call_count,
            "legacy_sites_sha256": self.legacy_sites_sha256,
        }
        if include_sites:
            payload["sites"] = [
                {**asdict(site), "kind": site.kind.value, "stable_id": site.stable_id}
                for site in self.sites
            ]
        return payload


@dataclass(frozen=True, slots=True)
class AuditActorProvenanceBaseline:
    legacy_call_count: int
    legacy_sites_sha256: str
    typed_call_count_floor: int

    @classmethod
    def from_path(cls, path: Path) -> AuditActorProvenanceBaseline:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("audit actor provenance baseline must be a JSON object")
        legacy_call_count = payload.get("legacy_call_count")
        legacy_sites_sha256 = payload.get("legacy_sites_sha256")
        typed_call_count_floor = payload.get("typed_call_count_floor")
        if (
            not isinstance(legacy_call_count, int)
            or isinstance(legacy_call_count, bool)
            or not isinstance(legacy_sites_sha256, str)
            or not isinstance(typed_call_count_floor, int)
            or isinstance(typed_call_count_floor, bool)
        ):
            raise ValueError(
                "audit actor provenance baseline fields have invalid types"
            )
        return cls(
            legacy_call_count=legacy_call_count,
            legacy_sites_sha256=legacy_sites_sha256,
            typed_call_count_floor=typed_call_count_floor,
        )


def _facade_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name) and node.id in AUDIT_FACADES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in AUDIT_FACADES:
        return node.attr
    return None


class _AuditActorCallVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.ordinals: dict[tuple[str, str], int] = {}
        self.sites: list[AuditActorCallSite] = []

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        facade = _facade_name(node.func)
        if facade is not None:
            scope = ".".join(self.scope) or "<module>"
            key = (scope, facade)
            ordinal = self.ordinals.get(key, 0) + 1
            self.ordinals[key] = ordinal
            typed = any(keyword.arg == "actor" for keyword in node.keywords)
            self.sites.append(
                AuditActorCallSite(
                    path=self.path,
                    scope=scope,
                    facade=facade,
                    ordinal=ordinal,
                    kind=(
                        AuditActorCallKind.TYPED if typed else AuditActorCallKind.LEGACY
                    ),
                )
            )
        self.generic_visit(node)


def collect_audit_actor_calls(project_root: Path) -> AuditActorProvenanceReport:
    sites: list[AuditActorCallSite] = []
    for family in ("app", "scripts"):
        root = project_root / family
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            relative_path = path.relative_to(project_root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - syntax has its own gate
                continue
            visitor = _AuditActorCallVisitor(relative_path)
            visitor.visit(tree)
            sites.extend(visitor.sites)
    return AuditActorProvenanceReport(
        sites=tuple(sorted(sites, key=lambda site: site.stable_id))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--include-sites",
        action="store_true",
        help="include the PII-free stable caller inventory in the JSON report",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even when the checked baseline does not match",
    )
    args = parser.parse_args()

    report = collect_audit_actor_calls(args.project_root.resolve())
    baseline = AuditActorProvenanceBaseline.from_path(args.baseline.resolve())
    matches = bool(
        report.legacy_call_count == baseline.legacy_call_count
        and report.legacy_sites_sha256 == baseline.legacy_sites_sha256
        and report.typed_call_count >= baseline.typed_call_count_floor
    )
    print(
        json.dumps(
            {
                **report.as_dict(include_sites=args.include_sites),
                "baseline_matches": matches,
            },
            sort_keys=True,
        )
    )
    return 0 if matches or args.report_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
