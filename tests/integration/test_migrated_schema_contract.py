"""Canaries for schema facts owned only by the real Alembic chain."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import DeviceStatus, OLTDevice

CONFIG_PACK_CONSTRAINT = "ck_olt_devices_config_pack_required"


def test_migrated_schema_contains_olt_config_pack_constraint(engine) -> None:
    names = {
        item.get("name")
        for item in inspect(engine).get_check_constraints("olt_devices")
    }

    assert CONFIG_PACK_CONSTRAINT in names, (
        "the database was not built from the deployed Alembic schema: "
        f"{CONFIG_PACK_CONSTRAINT} is migration-owned and absent from model metadata"
    )


def test_migrated_constraint_rejects_active_huawei_olt_without_config_pack(
    db_session: Session,
) -> None:
    invalid = OLTDevice(
        id=uuid4(),
        name=f"migration-canary-{uuid4().hex[:12]}",
        vendor="Huawei",
        status=DeviceStatus.active,
        is_active=True,
        config_pack={},
    )

    with pytest.raises(IntegrityError) as excinfo:
        with db_session.begin_nested():
            db_session.add(invalid)
            db_session.flush()

    assert CONFIG_PACK_CONSTRAINT in str(excinfo.value.orig)
