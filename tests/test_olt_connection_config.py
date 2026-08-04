"""Contract tests for the detached OLT connection config.

``OltConnectionConfig`` stands in for an ``OLTDevice`` on every SSH path that
runs outside a database transaction (autofind preflight, authorization writes).
Anything those paths read off the device must therefore be carried on the
detached config, or the call fails with ``AttributeError`` at the OLT edge.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from app.models.network import OLTDevice
from app.services.network.olt_protocol_adapters import OltConnectionConfig

# Attributes read off the device by olt_vendor_adapters.get_olt_adapter and
# huawei_command_profiles.get_huawei_command_profile.
_ADAPTER_IDENTITY_FIELDS = {
    "vendor",
    "model",
    "firmware_version",
    "software_version",
}


def _config() -> OltConnectionConfig:
    olt = SimpleNamespace(
        id=uuid4(),
        name="Jabi OLT",
        hostname="olt-jabi",
        mgmt_ip="10.0.0.1",
        vendor="Huawei",
        model="MA5608T",
        firmware_version=None,
        software_version=None,
        ssh_username="admin",
        ssh_password="secret",
        ssh_port=22,
    )
    return OltConnectionConfig.from_model(cast(OLTDevice, olt))


def test_config_carries_every_adapter_identity_field() -> None:
    fields = set(OltConnectionConfig.__dataclass_fields__)
    assert _ADAPTER_IDENTITY_FIELDS <= fields

    config = _config()
    for field in _ADAPTER_IDENTITY_FIELDS:
        assert hasattr(config, field)


def test_vendor_adapter_resolves_from_detached_config() -> None:
    from app.services.network.olt_vendor_adapters import get_olt_adapter

    adapter = get_olt_adapter(cast(OLTDevice, _config()))

    assert adapter.vendor_name.lower() == "huawei"
    assert adapter.supports_ssh()


def test_command_profile_resolves_from_detached_config() -> None:
    from app.services.network.huawei_command_profiles import (
        get_huawei_command_profile,
    )

    profile = get_huawei_command_profile(cast(OLTDevice, _config()))

    # MA5608T rejects scoped autofind; callers filter the global inventory.
    assert profile.supports_scoped_autofind is False
