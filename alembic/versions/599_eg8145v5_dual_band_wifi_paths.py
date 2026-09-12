"""Correct EG8145V5 TR-098 and dual-band WiFi password targets.

Revision ID: 599_eg8145v5_wifi
Revises: 598_opening_corrections
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "599_eg8145v5_wifi"
down_revision: str | None = "598_opening_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEEDED_CAPABILITY_ID = UUID("59900000-0000-4000-8000-000000000001")
_PRIMARY_PSK = "wifi.psk"
_ADDITIONAL_PSK = "wifi.psk.additional.1"
_TR098_ROOT = "InternetGatewayDevice"
_SECONDARY_PSK_PATH = "LANDevice.1.WLANConfiguration.5.PreSharedKey.1.PreSharedKey"
_NOTES = (
    "Enterprise GPON ONT. 4 ETH, dual-band WiFi, 2 VoIP. "
    "Deployed firmware exposes TR-098; primary 2.4 GHz and 5 GHz "
    "WLAN instances are 1 and 5."
)


def _capabilities() -> sa.TableClause:
    return sa.table(
        "vendor_model_capabilities",
        sa.column("id", sa.Uuid()),
        sa.column("vendor", sa.String()),
        sa.column("model", sa.String()),
        sa.column("firmware_pattern", sa.String()),
        sa.column("tr069_root", sa.String()),
        sa.column("supported_features", sa.JSON()),
        sa.column("max_wan_services", sa.Integer()),
        sa.column("max_lan_ports", sa.Integer()),
        sa.column("max_ssids", sa.Integer()),
        sa.column("supports_vlan_tagging", sa.Boolean()),
        sa.column("supports_qinq", sa.Boolean()),
        sa.column("supports_ipv6", sa.Boolean()),
        sa.column("is_active", sa.Boolean()),
        sa.column("notes", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _parameter_maps() -> sa.TableClause:
    return sa.table(
        "tr069_parameter_maps",
        sa.column("id", sa.Uuid()),
        sa.column("capability_id", sa.Uuid()),
        sa.column("canonical_name", sa.String()),
        sa.column("tr069_path", sa.String()),
        sa.column("writable", sa.Boolean()),
        sa.column("value_type", sa.String()),
        sa.column("notes", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _generic_capability_ids(bind, capabilities: sa.TableClause) -> list[UUID]:
    return list(
        bind.scalars(
            sa.select(capabilities.c.id).where(
                sa.func.lower(capabilities.c.vendor) == "huawei",
                sa.func.lower(capabilities.c.model) == "eg8145v5",
                capabilities.c.firmware_pattern.is_(None),
            )
        ).all()
    )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"vendor_model_capabilities", "tr069_parameter_maps"}.issubset(tables):
        return

    capabilities = _capabilities()
    parameter_maps = _parameter_maps()
    now = datetime.now(UTC)
    capability_ids = _generic_capability_ids(bind, capabilities)
    if len(capability_ids) > 1:
        raise RuntimeError(
            "Multiple generic Huawei EG8145V5 capabilities require review"
        )
    if capability_ids:
        capability_id = capability_ids[0]
        bind.execute(
            sa.update(capabilities)
            .where(capabilities.c.id == capability_id)
            .values(tr069_root=_TR098_ROOT, notes=_NOTES, updated_at=now)
        )
    else:
        capability_id = _SEEDED_CAPABILITY_ID
        bind.execute(
            sa.insert(capabilities).values(
                id=capability_id,
                vendor="Huawei",
                model="EG8145V5",
                firmware_pattern=None,
                tr069_root=_TR098_ROOT,
                supported_features={"wifi": True, "voip": True, "catv": False},
                max_wan_services=1,
                max_lan_ports=4,
                max_ssids=4,
                supports_vlan_tagging=True,
                supports_qinq=False,
                supports_ipv6=True,
                is_active=True,
                notes=_NOTES,
                created_at=now,
                updated_at=now,
            )
        )

    bind.execute(
        sa.delete(parameter_maps).where(
            parameter_maps.c.capability_id == capability_id,
            parameter_maps.c.canonical_name == _PRIMARY_PSK,
        )
    )
    existing_additional_id = bind.scalar(
        sa.select(parameter_maps.c.id).where(
            parameter_maps.c.capability_id == capability_id,
            parameter_maps.c.canonical_name == _ADDITIONAL_PSK,
        )
    )
    values = {
        "tr069_path": _SECONDARY_PSK_PATH,
        "writable": True,
        "value_type": "string",
        "notes": "5 GHz primary SSID shares the admitted WiFi password",
        "updated_at": now,
    }
    if existing_additional_id is None:
        bind.execute(
            sa.insert(parameter_maps).values(
                id=uuid4(),
                capability_id=capability_id,
                canonical_name=_ADDITIONAL_PSK,
                created_at=now,
                **values,
            )
        )
    else:
        bind.execute(
            sa.update(parameter_maps)
            .where(parameter_maps.c.id == existing_additional_id)
            .values(**values)
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"vendor_model_capabilities", "tr069_parameter_maps"}.issubset(tables):
        return

    capabilities = _capabilities()
    parameter_maps = _parameter_maps()
    now = datetime.now(UTC)
    capability_ids = _generic_capability_ids(bind, capabilities)
    if len(capability_ids) != 1:
        return
    capability_id = capability_ids[0]
    bind.execute(
        sa.delete(parameter_maps).where(
            parameter_maps.c.capability_id == capability_id,
            parameter_maps.c.canonical_name == _ADDITIONAL_PSK,
        )
    )
    if capability_id == _SEEDED_CAPABILITY_ID:
        bind.execute(sa.delete(capabilities).where(capabilities.c.id == capability_id))
        return

    bind.execute(
        sa.update(capabilities)
        .where(capabilities.c.id == capability_id)
        .values(
            tr069_root="Device",
            notes="Enterprise GPON ONT. 4 ETH, 4 WiFi, 2 VoIP. TR-181.",
            updated_at=now,
        )
    )
    bind.execute(
        sa.insert(parameter_maps).values(
            id=uuid4(),
            capability_id=capability_id,
            canonical_name=_PRIMARY_PSK,
            tr069_path="WiFi.AccessPoint.{i}.Security.KeyPassphrase",
            writable=True,
            value_type="string",
            notes="Huawei uses KeyPassphrase path",
            created_at=now,
            updated_at=now,
        )
    )
