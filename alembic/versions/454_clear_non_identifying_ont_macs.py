"""clear ONT MAC values that are not device identities

An OLT reports a locally administered placeholder for an ONT that has not
presented a real address. 227 of 1,523 units carried the SAME such value, and
because ``device_groups.resolve_device_id`` matches on MAC with ``.limit(1)``,
a lookup for it returned an arbitrary one of them. A further 468 units have no
MAC at all, so 46% of the fleet's MAC column was unusable as identity.

Measured before writing this: every locally administered value in the table was
that one placeholder, and every globally unique value was distinct. The
identity test therefore separates junk from real data exactly, with no false
positives on this fleet.

NULL is strictly better than a shared placeholder here. Absent identity is
recoverable and cannot be mistaken for a match; wrong identity silently
resolves the wrong device.

Only clears. It never invents a MAC, and it leaves globally unique values
untouched, including the one duplicated pair -- two units genuinely sharing a
burned-in address is a hardware or data question for the inventory owner, not
something a migration may decide.

Revision ID: 454_clear_non_identifying_ont_macs
Revises: 453_ipv4_primary_assignment_marker
Create Date: 2026-08-01
"""

import sqlalchemy as sa

from alembic import op

revision = "454_clear_non_identifying_ont_macs"
down_revision = "453_ipv4_primary_assignment_marker"
branch_labels = None
depends_on = None

#: First octet of a normalised MAC. Bit 0 marks a multicast/group address and
#: bit 1 marks a locally administered one; neither is a device identity.
_NOT_IDENTIFYING = """
    mac_address IS NOT NULL
    AND btrim(mac_address) <> ''
    AND (
        length(regexp_replace(upper(mac_address), '[^0-9A-F]', '', 'g')) <> 12
        OR ('x' || substr(regexp_replace(upper(mac_address), '[^0-9A-F]', '', 'g'), 1, 2))::bit(8)::int & 3 <> 0
        OR regexp_replace(upper(mac_address), '[^0-9A-F]', '', 'g') = '000000000000'
    )
"""


def upgrade() -> None:
    conn = op.get_bind()
    if not conn.dialect.name.startswith("postgres"):
        return

    cleared = conn.execute(
        sa.text(f"UPDATE ont_units SET mac_address = NULL WHERE {_NOT_IDENTIFYING}")
    ).rowcount
    print(
        f"[454] cleared {cleared} non-identifying ONT MAC value(s); "
        "absent identity is recoverable, wrong identity is not"
    )


def downgrade() -> None:
    # The cleared values were placeholders that identified nothing. Restoring
    # them would restore the collision, so this is deliberately irreversible.
    pass
