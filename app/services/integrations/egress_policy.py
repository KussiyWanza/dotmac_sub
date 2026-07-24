"""Egress allowlist an external connector is confined to.

Phase 4 of ADR 0005. A connector manifest declares the hosts it may reach
(``EgressManifest``). Those hosts are validated at install time but, before
this, were enforced nowhere at runtime: a running connector could reach any
host. ``EgressPolicy`` is the canonical, manifest-derived allowlist the
transport enforces.

The policy owns no decision — it is a projection of the manifest — and it is
deliberately default-deny: an empty allowlist means no network at all, not open
network. A connector that declares egress hosts cannot run until a gateway that
restricts outbound traffic to exactly those hosts is configured; the transport
fails closed rather than fall back to unrestricted egress.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.integrations.manifest import ConnectorManifest


@dataclass(frozen=True)
class EgressPolicy:
    """The set of hosts a connector may reach, derived from its manifest."""

    hosts: tuple[str, ...] = ()
    allow_installation_hosts: bool = False

    @classmethod
    def deny_all(cls) -> EgressPolicy:
        """No declared hosts: the connector gets no network."""
        return cls()

    @classmethod
    def from_manifest(cls, manifest: ConnectorManifest) -> EgressPolicy:
        return cls(
            hosts=tuple(sorted(manifest.egress.hosts)),
            allow_installation_hosts=manifest.egress.allow_installation_hosts,
        )

    @property
    def requires_network(self) -> bool:
        """Whether the connector needs any outbound network at all.

        False means the container runs with no network. True means it needs an
        allowlist egress gateway; the transport refuses to run it on an open
        network.
        """
        return bool(self.hosts) or self.allow_installation_hosts


__all__ = ["EgressPolicy"]
