"""Seed vendor model capabilities with TR-069 parameter map overrides.

Called at startup from ``settings_seed.py``.  Only inserts rows when the
``vendor_model_capabilities`` table is empty, so it is safe to call
repeatedly (idempotent).

Vendor-specific ``Tr069ParameterMap`` entries are only created where the
device's actual path **deviates** from the standard TR-181/TR-098 path.
Standard paths do not need DB rows — the ``Tr069PathResolver`` handles
them via its built-in dicts.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import Tr069ParameterMap, VendorModelCapability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed definitions
# ---------------------------------------------------------------------------

_VENDOR_SEEDS: list[dict[str, Any]] = [
    # ═══════════════════════════════════════════════════════════════════
    # Huawei EG Series (Enterprise Grade)
    # ═══════════════════════════════════════════════════════════════════
    {
        "vendor": "Huawei",
        "model": "EG8145V5",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": True,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": (
            "Enterprise GPON ONT. 4 ETH, dual-band WiFi, 2 VoIP. "
            "Deployed firmware exposes TR-098; primary 2.4 GHz and 5 GHz "
            "WLAN instances are 1 and 5."
        ),
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk.additional.1",
                "tr069_path": (
                    "LANDevice.1.WLANConfiguration.5.PreSharedKey.1.PreSharedKey"
                ),
                "writable": True,
                "value_type": "string",
                "notes": "5 GHz primary SSID shares the admitted WiFi password",
            },
        ],
    },
    # ═══════════════════════════════════════════════════════════════════
    # Huawei HG Series - Various models from SmartOLT
    # Source: docs/references/smartolt/Screenshot 2026-02-24 160837.png
    # ═══════════════════════════════════════════════════════════════════
    # ── HG8010H: 1 ETH, Bridging only ──────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8010H",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 1,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": False, "catv": False},
        "notes": "Single-port ONT. Bridging only. No WiFi/VoIP.",
        "parameter_overrides": [],
    },
    # ── HG8120C: 2 ETH, 1 VoIP ─────────────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8120C",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 2,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": True, "catv": False},
        "notes": "2-port ONT with VoIP. No WiFi.",
        "parameter_overrides": [],
    },
    # ── HG8240H: 4 ETH, 4 WiFi, 2 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8240H",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8240T: 4 ETH, 4 WiFi, 2 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8240T",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT variant. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8242: 4 ETH, 4 WiFi, 2 VoIP, 1 CATV ──────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8242",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": True},
        "notes": "4-port ONT with CATV support. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8242H: 4 ETH, 4 WiFi, 2 VoIP, 1 CATV ─────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8242H",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": True},
        "notes": "4-port ONT with CATV support variant. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8245H: 4 ETH, 4 WiFi, 2 VoIP (common model) ──────────────────
    {
        "vendor": "Huawei",
        "model": "HG8245H",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "Common 4-port dual-band WiFi ONT with VoIP. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "system.mac_address",
                "tr069_path": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.MACAddress",
                "writable": False,
                "value_type": "string",
                "notes": "Huawei exposes MAC on WAN PPP connection",
            },
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8310M: 1 ETH, Bridging only ──────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8310M",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 1,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": False, "catv": False},
        "notes": "Single-port ONT. Bridging only. No WiFi/VoIP.",
        "parameter_overrides": [],
    },
    # ── HG8311: 1 ETH, 1 VoIP ──────────────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8311",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 1,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": True, "catv": False},
        "notes": "Single-port ONT with VoIP. No WiFi.",
        "parameter_overrides": [],
    },
    # ── HG8321R: 2 ETH, 1 VoIP ─────────────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8321R",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 2,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": True, "catv": False},
        "notes": "2-port ONT with VoIP. No WiFi.",
        "parameter_overrides": [],
    },
    # ── HG8326R: 2 ETH, 4 WiFi, 1 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8326R",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 2,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "2-port ONT with dual-band WiFi and VoIP.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8340M: 4 ETH, no WiFi ────────────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8340M",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": False, "catv": False},
        "notes": "4-port ONT without WiFi. For wired-only deployments.",
        "parameter_overrides": [],
    },
    # ── HG8346M: 4 ETH, 4 WiFi, 2 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8346M",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT with VoIP. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8346R: 4 ETH, 4 WiFi, 2 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8346R",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT variant. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8347R: 4 ETH, 4 WiFi, 1 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8347R",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT with single VoIP. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8545M: 4 ETH, 4 WiFi, 1 VoIP ─────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8545M",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT with VoIP. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8546M: 4 ETH, 4 WiFi, 1 VoIP (common model) ──────────────────
    {
        "vendor": "Huawei",
        "model": "HG8546M",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "Common 4-port dual-band WiFi ONT with VoIP. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG865: 4 ETH, 4 WiFi, 2 VoIP ───────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG865",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": True, "voip": True, "catv": False},
        "notes": "4-port dual-band WiFi ONT with VoIP. TR-098.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "LANDevice.1.WLANConfiguration.{i}.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase, not PreSharedKey",
            },
        ],
    },
    # ── HG8145V5: 4 ETH, 2 WiFi (TR-181) ────────────────────────────────
    {
        "vendor": "Huawei",
        "model": "HG8145V5",
        "tr069_root": "Device",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 2,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": True,
        "supported_features": {"wifi": True, "voip": False, "catv": False},
        "notes": "Newer GPON ONT with IPv6 support. TR-181 data model.",
        "parameter_overrides": [
            {
                "canonical_name": "wifi.psk",
                "tr069_path": "WiFi.AccessPoint.{i}.Security.KeyPassphrase",
                "writable": True,
                "value_type": "string",
                "notes": "Huawei uses KeyPassphrase path",
            },
        ],
    },
    # ═══════════════════════════════════════════════════════════════════
    # ZTE ONT Models
    # ═══════════════════════════════════════════════════════════════════
    {
        "vendor": "ZTE",
        "model": "F660",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 4,
        "max_lan_ports": 4,
        "max_ssids": 4,
        "supports_vlan_tagging": True,
        "supports_qinq": True,
        "supports_ipv6": True,
        "supported_features": {"wifi": True, "voip": True, "catv": True},
        "notes": "ZTE GPON ONT with multiple WAN services. TR-098.",
        "parameter_overrides": [],
    },
    # ═══════════════════════════════════════════════════════════════════
    # Nokia ONT Models
    # ═══════════════════════════════════════════════════════════════════
    {
        "vendor": "Nokia",
        "model": "G-010G-A",
        "tr069_root": "Device",
        "max_wan_services": 1,
        "max_lan_ports": 1,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": True,
        "supported_features": {"wifi": False, "voip": False, "catv": False},
        "notes": "Nokia SFP ONT (no WiFi, single LAN). TR-181.",
        "parameter_overrides": [],
    },
    # ═══════════════════════════════════════════════════════════════════
    # Generic / Fallback
    # ═══════════════════════════════════════════════════════════════════
    {
        "vendor": "Generic",
        "model": "default",
        "tr069_root": "InternetGatewayDevice",
        "max_wan_services": 1,
        "max_lan_ports": 4,
        "max_ssids": 0,
        "supports_vlan_tagging": True,
        "supports_qinq": False,
        "supports_ipv6": False,
        "supported_features": {"wifi": False, "voip": False, "catv": False},
        "notes": "Generic fallback for unknown ONT models.",
        "parameter_overrides": [],
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_vendor_capabilities(db: Session) -> int:
    """Populate vendor model capabilities if the table is empty.

    Returns:
        Number of capability rows inserted.
    """
    count = db.scalar(select(func.count()).select_from(VendorModelCapability))
    if count and count > 0:
        logger.debug(
            "Vendor capabilities table already has %d rows, skipping seed.",
            count,
        )
        return 0

    inserted = 0
    for seed in _VENDOR_SEEDS:
        seed = dict(seed)  # shallow copy to avoid mutating module-level data
        overrides = seed.pop("parameter_overrides", [])
        cap = VendorModelCapability(**seed)
        db.add(cap)
        db.flush()  # get cap.id for FK

        for override in overrides:
            param_map = Tr069ParameterMap(
                capability_id=cap.id,
                **override,
            )
            db.add(param_map)

        inserted += 1
        logger.info(
            "Seeded vendor capability: %s %s (root: %s, %d overrides)",
            cap.vendor,
            cap.model,
            cap.tr069_root,
            len(overrides),
        )

    db.commit()
    logger.info("Seeded %d vendor model capabilities.", inserted)
    return inserted
