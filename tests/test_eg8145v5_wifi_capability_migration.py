from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/599_eg8145v5_dual_band_wifi_paths.py"
    )
    spec = importlib.util.spec_from_file_location("migration_599", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vendor_seeds() -> list[dict[str, object]]:
    path = Path(__file__).resolve().parents[1] / "app/services/network/tr069_seed.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_VENDOR_SEEDS"
            and node.value is not None
        ):
            return cast(list[dict[str, object]], ast.literal_eval(node.value))
    raise AssertionError("_VENDOR_SEEDS declaration not found")


def _tables(connection):
    metadata = sa.MetaData()
    capabilities = sa.Table(
        "vendor_model_capabilities",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("vendor", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("firmware_pattern", sa.String()),
        sa.Column("tr069_root", sa.String()),
        sa.Column("supported_features", sa.JSON()),
        sa.Column("max_wan_services", sa.Integer()),
        sa.Column("max_lan_ports", sa.Integer()),
        sa.Column("max_ssids", sa.Integer()),
        sa.Column("supports_vlan_tagging", sa.Boolean()),
        sa.Column("supports_qinq", sa.Boolean()),
        sa.Column("supports_ipv6", sa.Boolean()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    parameter_maps = sa.Table(
        "tr069_parameter_maps",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("capability_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("tr069_path", sa.String(), nullable=False),
        sa.Column("writable", sa.Boolean()),
        sa.Column("value_type", sa.String()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(connection)
    return capabilities, parameter_maps


def test_seed_declares_eg8145v5_tr098_dual_band_password_targets() -> None:
    seed = next(
        item
        for item in _vendor_seeds()
        if item["vendor"] == "Huawei" and item["model"] == "EG8145V5"
    )

    assert seed["tr069_root"] == "InternetGatewayDevice"
    assert seed["parameter_overrides"] == [
        {
            "canonical_name": "wifi.psk.additional.1",
            "tr069_path": (
                "LANDevice.1.WLANConfiguration.5.PreSharedKey.1.PreSharedKey"
            ),
            "writable": True,
            "value_type": "string",
            "notes": "5 GHz primary SSID shares the admitted WiFi password",
        }
    ]


def test_migration_repairs_existing_eg8145v5_capability(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        capabilities, parameter_maps = _tables(connection)
        capability_id = uuid4()
        now = datetime.now(UTC)
        connection.execute(
            capabilities.insert().values(
                id=capability_id,
                vendor="Huawei",
                model="EG8145V5",
                firmware_pattern=None,
                tr069_root="Device",
                supported_features={"wifi": True},
                max_wan_services=1,
                max_lan_ports=4,
                max_ssids=4,
                supports_vlan_tagging=True,
                supports_qinq=False,
                supports_ipv6=True,
                is_active=True,
                notes="old",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            parameter_maps.insert().values(
                id=uuid4(),
                capability_id=capability_id,
                canonical_name="wifi.psk",
                tr069_path="WiFi.AccessPoint.{i}.Security.KeyPassphrase",
                writable=True,
                value_type="string",
                notes="old",
                created_at=now,
                updated_at=now,
            )
        )
        migration = _load_migration()
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()

        capability = (
            connection.execute(
                sa.select(capabilities).where(capabilities.c.id == capability_id)
            )
            .mappings()
            .one()
        )
        maps = (
            connection.execute(
                sa.select(parameter_maps)
                .where(parameter_maps.c.capability_id == capability_id)
                .order_by(parameter_maps.c.canonical_name)
            )
            .mappings()
            .all()
        )
        assert capability["tr069_root"] == "InternetGatewayDevice"
        assert [item["canonical_name"] for item in maps] == ["wifi.psk.additional.1"]
        assert maps[0]["tr069_path"].endswith(
            "WLANConfiguration.5.PreSharedKey.1.PreSharedKey"
        )

        migration.downgrade()

        restored = (
            connection.execute(
                sa.select(capabilities).where(capabilities.c.id == capability_id)
            )
            .mappings()
            .one()
        )
        restored_maps = (
            connection.execute(
                sa.select(parameter_maps).where(
                    parameter_maps.c.capability_id == capability_id
                )
            )
            .mappings()
            .all()
        )
        assert restored["tr069_root"] == "Device"
        assert [item["canonical_name"] for item in restored_maps] == ["wifi.psk"]


def test_migration_seeds_missing_eg8145v5_capability(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        capabilities, parameter_maps = _tables(connection)
        migration = _load_migration()
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()

        capability = connection.execute(sa.select(capabilities)).mappings().one()
        parameter_map = connection.execute(sa.select(parameter_maps)).mappings().one()
        assert capability["tr069_root"] == "InternetGatewayDevice"
        assert parameter_map["capability_id"] == capability["id"]
        assert parameter_map["canonical_name"] == "wifi.psk.additional.1"

        migration.downgrade()

        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(capabilities)) == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(parameter_maps))
            == 0
        )
