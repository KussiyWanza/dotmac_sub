"""Shared helper functions for network services.

These are re-exported from app.services.common for backwards compatibility.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.services.common import (
    apply_ordering as _apply_ordering,
)
from app.services.common import (
    apply_pagination as _apply_pagination,
)
from app.services.common import (
    validate_enum as _validate_enum,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from sqlalchemy.sql import Select

logger = logging.getLogger(__name__)

__all__ = [
    "_apply_ordering",
    "_apply_pagination",
    "_validate_enum",
    "NasTarget",
    "SubscriberOwnerValidator",
    "SubscriberTemplateContextProvider",
    "SubscriberValidator",
    "decode_huawei_hex_serial",
    "encode_to_hex_serial",
    "normalize_mac_address",
]


class SubscriberValidator(Protocol):
    """Cross-domain bridge for OLT/ONT services that need subscriber info.

    The network package must not import ``app.models.subscriber`` directly.
    Callers inject an implementation of this protocol (typically
    ``app.services.network_subscriber_bridge.DefaultSubscriberValidator``)
    when subscriber integration is desired. A ``None`` validator means the
    network service runs in standalone mode and skips subscriber checks.
    """

    def validate_assignment_customer_links(
        self,
        db: Session,
        *,
        subscriber_id: object | None,
        service_address_id: object | None,
    ) -> None:
        """Validate that an ONT assignment's subscriber/service address pair is consistent.

        Raises ``HTTPException`` on failure; returns ``None`` on success or when
        there is nothing to validate (e.g. both identifiers are ``None``).
        """
        ...

    def resolve_assignment_subscription(
        self,
        db: Session,
        *,
        subscription_id: object,
        subscriber_id: object | None,
    ) -> tuple[object, object]:
        """Validate a catalog subscription binding and return its owner."""
        ...

    def validate_active_assignment_subscription(
        self,
        db: Session,
        *,
        subscription_id: object,
        subscriber_id: object,
    ) -> tuple[object, object]:
        """Validate that a subscription and its subscriber are active and consistent."""
        ...

    def apply_subscription_device_intent(
        self,
        db: Session,
        *,
        subscription_id: object,
        ont: Any,
    ) -> bool:
        """Apply staged provisioning intent to an ONT desired-state row."""
        ...

    def augment_ont_search(
        self,
        stmt: Select,
        term: str,
        *,
        assignment_alias: Any,
    ) -> tuple[Select, Sequence[Any]]:
        """Augment an ONT search statement with subscriber joins and conditions.

        Given the in-progress ``Select`` and the ILIKE-wrapped ``term``, this
        returns the (possibly) augmented statement plus a list of extra SQL
        clause elements that should be OR'd into the main search ``where``.
        Implementations that don't support subscriber search may return the
        statement unchanged and an empty sequence.
        """
        ...


class SubscriberOwnerValidator(Protocol):
    """Bridge for network services that need business-owner subscriber checks."""

    def validate_business_owner(
        self,
        db: Session,
        *,
        owner_subscriber_id: object | None,
    ) -> None:
        """Validate that the supplied owner is a valid business subscriber."""
        ...


class SubscriberTemplateContextProvider(Protocol):
    """Bridge for subscriber-owned template fields used by network profiles."""

    def get_template_context(
        self,
        db: Session,
        *,
        subscriber_id: object,
    ) -> dict[str, str]:
        """Return subscriber fields safe for network template rendering."""
        ...


@dataclass(frozen=True, kw_only=True)
class NasTarget:
    """Lightweight DTO describing a NAS device for provisioning operations.

    Used by network-domain services to avoid importing ``app.models.catalog``.
    Callers that hold a ``NasDevice`` ORM row should construct one of these
    inline from the fields below.

    Attributes match the fields read by the MikroTik VLAN/PPPoE provisioning
    helpers (``app.services.nas._mikrotik_vlan``) so the DTO can be handed
    straight through to those helpers without needing the ORM row itself.
    """

    name: str
    vendor: Any
    management_ip: str | None = None
    ip_address: str | None = None
    api_username: str | None = None
    api_password: str | None = None
    tags: Any = None


def decode_huawei_hex_serial(value: str | None) -> str | None:
    """Decode a 16-char hex serial into a human-readable vendor+serial form.

    Huawei (and similar) OLTs sometimes report ONT serials as 16 hex digits
    where the first 8 hex chars are the ASCII vendor prefix.  For example,
    ``485754437D4701C3`` decodes to ``HWTC7D4701C3``.

    Returns the decoded serial or ``None`` if the value is not a valid
    hex-encoded vendor serial.
    """
    raw = str(value or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]{16}", raw):
        if raw:
            logger.debug("Invalid Huawei hex serial format: %r", value)
        return None
    try:
        vendor_ascii = bytes.fromhex(raw[:8]).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        logger.warning("Malformed Huawei hex serial vendor prefix: %r", value)
        return None
    if vendor_ascii.isalpha():
        return f"{vendor_ascii}{raw[8:]}"
    logger.warning("Huawei hex serial vendor prefix is not alphabetic: %r", value)
    return None


def encode_to_hex_serial(value: str | None) -> str | None:
    """Encode a vendor+serial form to 16-char hex serial.

    Converts human-readable ONT serials like ``HWTCA31A3529`` or ``HWTC-A31A3529``
    into the full 16-character hex form ``48575443A31A3529``.

    Returns the hex serial or ``None`` if the value cannot be encoded.
    """
    raw = str(value or "").strip().upper()
    if not raw:
        return None

    # Already a valid 16-char hex serial
    if re.fullmatch(r"[0-9A-F]{16}", raw):
        return raw

    # Remove common separators (dash, colon, space)
    raw = re.sub(r"[-:\s]", "", raw)

    # Check for vendor prefix pattern (4 ASCII letters + 8 hex digits)
    match = re.fullmatch(r"([A-Z]{4})([0-9A-F]{8})", raw)
    if not match:
        return None

    vendor_prefix = match.group(1)
    serial_suffix = match.group(2)

    try:
        vendor_hex = vendor_prefix.encode("ascii").hex().upper()
    except (ValueError, UnicodeEncodeError):
        return None

    return f"{vendor_hex}{serial_suffix}"


def normalize_mac_address(value: str | None) -> str | None:
    """Return a canonical uppercase colon-separated MAC address."""
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(compact) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        return None
    compact = compact.upper()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def is_identifying_mac_address(value: str | None) -> bool:
    """Whether a MAC may be stored as a DEVICE IDENTITY.

    A well-formed MAC is not automatically an identity. The second-least
    significant bit of the first octet marks a locally administered address --
    assigned by software rather than burned in by a vendor -- so it carries no
    uniqueness guarantee and several devices can legitimately share one. The
    multicast bit marks a group address, which is never a device.

    This is not a hypothetical distinction here. An OLT reported the same
    locally administered address for 227 ONTs, and because
    ``device_groups.resolve_device_id`` matches on MAC with ``.limit(1)``, a
    lookup for it returned an arbitrary one of them. Measured across the fleet
    the split is exact: every locally administered value was that one
    placeholder, and every globally unique value was distinct.

    Storing such a value is worse than storing nothing, because NULL cannot be
    mistaken for a match.
    """
    normalized = normalize_mac_address(value)
    if normalized is None:
        return False
    first_octet = int(normalized[:2], 16)
    if first_octet & 0b1:  # multicast/group address
        return False
    if first_octet & 0b10:  # locally administered
        return False
    return int(normalized.replace(":", ""), 16) != 0
