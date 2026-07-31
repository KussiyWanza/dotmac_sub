from types import SimpleNamespace

import pytest

from app.services.network.huawei_command_profiles import get_huawei_command_profile


def test_ma5608t_v800r013_uses_space_separated_display_ont_info():
    olt = SimpleNamespace(
        model="MA5608T",
        firmware_version="V800R013C00 SPC105",
        software_version=None,
    )

    profile = get_huawei_command_profile(olt)

    assert profile.requires_slow_send is True
    assert profile.supports_scoped_autofind is False
    assert profile.display_ont_info("0/1/7", 5) == "display ont info 0 1 7 5"
    assert (
        profile.display_ont_optical_info("0/1/7", 5)
        == "display ont optical-info 0 1 7 5"
    )
    assert profile.display_ont_info_all("0/1") == "display ont info 0 1 all"
    assert (
        profile.display_ont_status_inventory("0/1/12") == "display ont info 0 1 12 all"
    )
    with pytest.raises(ValueError):
        profile.display_ont_info("0/1", 0)


def test_ma5800_v100r019_uses_slash_fsp_display_ont_info():
    olt = SimpleNamespace(
        model="MA5800-X2",
        firmware_version="V100R019C11 SPH216",
        software_version=None,
    )

    profile = get_huawei_command_profile(olt)

    assert profile.requires_slow_send is False
    assert profile.supports_scoped_autofind is True
    assert profile.display_ont_info("0/1", 0) == "display ont info 0/1 0"
    assert profile.display_ont_info_all("0/1") == "display ont info 0/1 all"
    assert (
        profile.display_ont_status_inventory("0/2/1")
        == "display ont info summary 0/2/1"
    )
    assert profile.display_ont_info("0/1/0", 5) == "display ont info 0/1 0 5"
    assert (
        profile.display_ont_optical_info("0/1/0", 5)
        == "display ont optical-info 0/1 0 5"
    )
