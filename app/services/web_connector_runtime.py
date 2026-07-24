"""Read-only operator view of the connector runtime posture.

Phase 5 of ADR 0005. Surfaces, for every registered connector definition, its
runtime trust tier and — for out-of-process connectors — the image, digest,
egress confinement, and resource bounds it would run under, plus whether the
tier is executable in this deployment.

This is a projection, not a control panel. It owns no decision: executability is
read from the same `resolve_runner` the runtime uses, and every fact comes from
the code-owned manifest or the installations owner. Mutating controls — install
or upgrade a connector by digest — are deliberately absent while the external
tier is not yet executable (it fails closed until the egress gateway of Phase 4b
and a first connector in Phase 6). Offering a control for something that cannot
run would mislead the operator, so the screen shows posture and says plainly
that external connectors are registrable but not yet executable here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.integrations import installations
from app.services.integrations.egress_policy import EgressPolicy
from app.services.integrations.manifest import ConnectorManifest, ConnectorRuntimeType
from app.services.integrations.registry import connector_definitions
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    resolve_runner,
)

_TIER_LABELS = {
    ConnectorRuntimeType.builtin_worker: "Built-in",
    ConnectorRuntimeType.legacy_adapter: "Legacy adapter",
    ConnectorRuntimeType.external_oci: "External (OCI)",
    ConnectorRuntimeType.catalogue_only: "Catalogue only",
}


def _executability(manifest: ConnectorManifest) -> tuple[bool, str | None]:
    """Whether this connector's declared tier can execute here, and why not.

    Read from the same resolver the runtime uses, so the screen can never claim
    a connector is runnable when a real operation would be refused.
    """
    try:
        resolve_runner(manifest)
    except RuntimeExecutionError as exc:
        return False, str(exc)
    return True, None


def _egress_summary(manifest: ConnectorManifest) -> str:
    policy = EgressPolicy.from_manifest(manifest)
    if not policy.requires_network:
        return "No network (default-deny)"
    if policy.hosts:
        return f"{len(policy.hosts)} allowed host(s): " + ", ".join(policy.hosts)
    return "Installation host (per config)"


def _image_reference(manifest: ConnectorManifest) -> str | None:
    runtime = manifest.runtime
    if runtime.image and runtime.digest:
        return f"{runtime.image}@{runtime.digest}"
    return runtime.image


def _install_counts(db: Session, connector_key: str) -> tuple[int, int]:
    rows = installations.list_installations(db, connector_key=connector_key, limit=200)
    installed = [r for r in rows if r.state != "retired"]
    enabled = [r for r in installed if r.state == "enabled"]
    return len(installed), len(enabled)


def build_runtime_posture(db: Session) -> dict[str, Any]:
    """Project every connector definition onto its runtime posture."""

    rows: list[dict[str, Any]] = []
    for manifest in sorted(connector_definitions(), key=lambda m: m.name.lower()):
        executable, reason = _executability(manifest)
        installed, enabled = _install_counts(db, manifest.key)
        is_external = manifest.runtime.type is ConnectorRuntimeType.external_oci
        rows.append(
            {
                "key": manifest.key,
                "name": manifest.name,
                "version": manifest.version,
                "tier": manifest.runtime.type.value,
                "tier_label": _TIER_LABELS.get(
                    manifest.runtime.type, manifest.runtime.type.value
                ),
                "is_external": is_external,
                "executable": executable,
                "not_executable_reason": reason,
                "image_reference": _image_reference(manifest) if is_external else None,
                "manifest_digest": manifest.digest,
                "egress_summary": _egress_summary(manifest),
                "capability_count": len(manifest.capabilities),
                "installed_count": installed,
                "enabled_count": enabled,
            }
        )

    external = [r for r in rows if r["is_external"]]
    return {
        "connectors": rows,
        "stats": {
            "total": len(rows),
            "external": len(external),
            "external_executable": sum(1 for r in external if r["executable"]),
        },
        # Surfaced so the template can state the platform position plainly rather
        # than implying external connectors are ready to run.
        "external_tier_live": any(r["executable"] for r in external),
    }


__all__ = ["build_runtime_posture"]
