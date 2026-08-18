"""Inversion adapter exposing catalog IP-block policy to network owners.

The network package cannot depend directly on the subscription/catalog domain.
This higher-layer bridge keeps that dependency outside the network boundary
while preserving typed inputs for the ONT configuration owner.
"""

from app.services.catalog.ip_block_choices import (
    IpBlockPrefix,
    active_catalog_ip_block_choices,
    subscriber_ip_block_entitlements,
)

__all__ = [
    "IpBlockPrefix",
    "active_catalog_ip_block_choices",
    "subscriber_ip_block_entitlements",
]
