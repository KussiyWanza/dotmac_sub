"""Canonical SOT declarations for the vpn_remote_access domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="vpn_remote_access",
    services=(
        SOTService(
            name="vpn.key_material",
            module="app.services.wireguard_crypto",
            owns=(
                "WireGuard keypair generation",
                "private-key at-rest encryption",
            ),
        ),
        SOTService(
            name="vpn.system_interface",
            module="app.services.wireguard_system",
            owns=(
                "VPS-local WireGuard interface state",
                "system peer projection to the running interface",
            ),
            depends_on=("vpn.key_material",),
        ),
        SOTService(
            name="vpn.wireguard",
            module="app.services.wireguard",
            owns=(
                "WireGuard server and peer lifecycle",
                "peer config and MikroTik RouterOS script generation",
            ),
            depends_on=("vpn.system_interface", "vpn.key_material"),
        ),
        SOTService(
            name="vpn.routing_readiness",
            module="app.services.vpn_routing",
            owns=("VPN interface readiness for device access",),
            depends_on=("vpn.system_interface",),
        ),
    ),
    entrypoints=(
        "app.api.wireguard",
        "app.tasks.wireguard",
        "app.services.web_vpn_servers",
        "app.services.web_vpn_peers",
        "app.services.web_vpn_management",
    ),
    rule="Admin VPN routes and device-access callers resolve WireGuard "
    "server/peer lifecycle, config and RouterOS script generation, key "
    "material, and interface readiness through these owners. web_vpn_* "
    "adapters and device-access code do not build WireGuard config, "
    "mutate peers, or write the system interface directly. The Redis "
    "vpn_cache is a rebuildable projection, never a source of truth.",
)
