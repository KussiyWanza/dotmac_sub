"""Shared machinery for the /api/v1 OpenAPI contract-surface pin.

The full OpenAPI document is ~4 MB across 2,200+ paths (JSON API, admin web,
portals), too large to snapshot reviewably. The pinned *contract surface* is
the external `/api/v1` API only: per route — method, params, request/response
schema fingerprints; plus a fingerprint per transitively referenced component
schema. A fingerprint change names the exact route/schema that moved, and the
committed manifest diffs at route granularity in review.

Consumers:
- ``tests/architecture/test_openapi_contract_surface.py`` — fails on drift.
- ``scripts/update_openapi_contract.py`` — regenerates the manifest after an
  intentional contract change.

The app is built the same way ``tests/test_api_happy_path.py`` builds its
sweep app: every core + deferred API router mounted on a fresh FastAPI
instance, no lifespan, no network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("openapi_contract_surface.json")
API_PREFIX = "/api/v1"
_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


def build_full_app():
    """Mount every core + deferred API router on an isolated app."""
    from fastapi import FastAPI

    from app.main import (
        _CORE_ROUTER_SPECS,
        _DEFERRED_API_ROUTER_SPECS,
        _load_router_object,
        _mount_router,
    )

    app = FastAPI(title="openapi-contract-surface")
    failures: list[tuple[str, str]] = []
    for spec in (*_CORE_ROUTER_SPECS, *_DEFERRED_API_ROUTER_SPECS):
        module, attr, kind, mode = spec
        try:
            router = _load_router_object(module, attr)
            _mount_router(app, router, kind, mode)
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller
            failures.append((f"{module}:{attr}", repr(exc)))
    if failures:
        raise RuntimeError(f"router mount failures: {failures}")
    return app


def _fingerprint(node: Any) -> str:
    return hashlib.sha256(
        json.dumps(node, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]


def _collect_refs(node: Any, acc: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            acc.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _collect_refs(value, acc)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, acc)


def compute_surface(app) -> dict[str, Any]:
    doc = app.openapi()
    schemas = doc.get("components", {}).get("schemas", {})

    routes: dict[str, Any] = {}
    referenced: set[str] = set()
    for path, item in doc.get("paths", {}).items():
        if not path.startswith(API_PREFIX):
            continue
        for method in _METHODS:
            op = item.get(method)
            if op is None:
                continue
            _collect_refs(op, referenced)
            params = sorted(
                f"{p.get('in')}:{p.get('name')}{'!' if p.get('required') else ''}"
                for p in op.get("parameters", [])
            )
            request = op.get("requestBody")
            responses = {
                code: _fingerprint(body)
                for code, body in sorted(op.get("responses", {}).items())
            }
            routes[f"{method.upper()} {path}"] = {
                "params": params,
                "request": _fingerprint(request) if request is not None else None,
                "responses": responses,
            }

    # Transitive closure: a referenced schema's own $refs are part of the
    # contract too (a nested field change must move some fingerprint).
    frontier = set(referenced)
    while frontier:
        name = frontier.pop()
        node = schemas.get(name)
        if node is None:
            continue
        nested: set[str] = set()
        _collect_refs(node, nested)
        frontier |= nested - referenced
        referenced |= nested

    return {
        "api_prefix": API_PREFIX,
        "routes": routes,
        "schemas": {
            name: _fingerprint(schemas[name])
            for name in sorted(referenced)
            if name in schemas
        },
    }


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def write_manifest(surface: dict[str, Any]) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(surface, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )


def diff_surfaces(pinned: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Human-actionable drift lines, bounded by the caller."""
    lines: list[str] = []
    for kind in ("routes", "schemas"):
        old, new = pinned.get(kind, {}), current.get(kind, {})
        for key in sorted(set(old) - set(new)):
            lines.append(f"removed {kind[:-1]}: {key}")
        for key in sorted(set(new) - set(old)):
            lines.append(f"added {kind[:-1]}: {key}")
        for key in sorted(set(old) & set(new)):
            if old[key] != new[key]:
                lines.append(
                    f"changed {kind[:-1]}: {key}; "
                    f"pinned={json.dumps(old[key], sort_keys=True)}; "
                    f"current={json.dumps(new[key], sort_keys=True)}"
                )
    return lines
