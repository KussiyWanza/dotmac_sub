"""WiFi CWMP path helpers shared by reader and planner."""

from __future__ import annotations

import re
from dataclasses import replace

from .state import Tr069WifiParameterPaths


def wifi_paths_for_instance(
    paths: Tr069WifiParameterPaths,
    root: str | None,
    instance_index: int,
) -> Tr069WifiParameterPaths:
    """Retarget standard/model WiFi paths to the active WLAN instance."""
    if instance_index < 1:
        return paths

    def retarget(path: str) -> str:
        if root == "Device":
            for object_name in ("SSID", "Radio", "AccessPoint"):
                path = re.sub(
                    rf"(\.WiFi\.{object_name}\.)\d+(\.)",
                    rf"\g<1>{instance_index}\2",
                    path,
                    count=1,
                )
            return path
        return re.sub(
            r"(\.WLANConfiguration\.)\d+(\.)",
            rf"\g<1>{instance_index}\2",
            path,
            count=1,
        )

    return replace(
        paths,
        enabled=retarget(paths.enabled),
        ssid=retarget(paths.ssid),
        psk_path=(
            paths.psk_path if paths.additional_psk_paths else retarget(paths.psk_path)
        ),
        channel=retarget(paths.channel),
        security_mode=retarget(paths.security_mode),
    )


__all__ = ("wifi_paths_for_instance",)
