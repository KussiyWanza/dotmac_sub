"""Operator-selectable ONT LAN block-size choices."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.network_catalog_ip_block_bridge import IpBlockPrefix


@dataclass(frozen=True, slots=True)
class OntLanBlockPrefixChoice:
    prefix: IpBlockPrefix
    subnet_mask: str
    address_count: int


OPERATOR_LAN_BLOCK_PREFIXES: tuple[IpBlockPrefix, ...] = (
    IpBlockPrefix.p30,
    IpBlockPrefix.p29,
    IpBlockPrefix.p28,
    IpBlockPrefix.p27,
    IpBlockPrefix.p26,
    IpBlockPrefix.p25,
    IpBlockPrefix.p24,
)


def operator_lan_block_prefix_choices() -> tuple[OntLanBlockPrefixChoice, ...]:
    return tuple(
        OntLanBlockPrefixChoice(
            prefix=prefix,
            subnet_mask=prefix.subnet_mask,
            address_count=prefix.address_count,
        )
        for prefix in OPERATOR_LAN_BLOCK_PREFIXES
    )


__all__ = [
    "OPERATOR_LAN_BLOCK_PREFIXES",
    "OntLanBlockPrefixChoice",
    "operator_lan_block_prefix_choices",
]
