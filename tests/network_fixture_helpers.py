"""Schema-honest network setup shared by database-backed tests."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.catalog import RegionZone
from app.models.network import OLTDevice, Vlan, VlanPurpose


def attach_test_olt_config_pack(
    db: Session,
    *,
    olt: OLTDevice,
    region: RegionZone,
) -> None:
    """Attach real VLAN rows and a resolver-valid config pack to an OLT.

    The migrated PostgreSQL schema requires the complete config-pack key set,
    while the production resolver also requires the VLAN values
    to be real UUID-backed rows.  A string that merely satisfies the JSONB
    CHECK is not a valid fixture because it fails as soon as application code
    resolves the pack.
    """

    # Allocate all identifiers before the first flush. PostgreSQL's OLT
    # config-pack CHECK is unconditional, while the pack itself references
    # VLAN rows that point back to the OLT; flushing either side first would
    # therefore create an impossible fixture-ordering cycle.
    if region.id is None:
        region.id = uuid.uuid4()
    if olt.id is None:
        olt.id = uuid.uuid4()

    internet_vlan_id = uuid.uuid4()
    management_vlan_id = uuid.uuid4()

    internet_vlan = Vlan(
        id=internet_vlan_id,
        region_id=region.id,
        olt_device_id=olt.id,
        tag=100,
        name="Test Internet",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    management_vlan = Vlan(
        id=management_vlan_id,
        region_id=region.id,
        olt_device_id=olt.id,
        tag=802,
        name="Test Management",
        purpose=VlanPurpose.management,
        is_active=True,
    )
    olt.config_pack = {
        "line_profile_id": 1,
        "service_profile_id": 1,
        "internet_vlan_id": str(internet_vlan_id),
        "management_vlan_id": str(management_vlan_id),
        "tr069_olt_profile_id": 2,
        "mgmt_gem_index": 2,
        "internet_gem_index": 1,
    }
    db.add_all([region, olt, internet_vlan, management_vlan])
    db.flush()
