"""Merge staff-session and ticket-comment migration heads.

Revision ID: 541_merge_staff_session_and_ticket_comment_heads
Revises: 540_staff_session_party_ratchet, 540_ticket_comment_mentions
Create Date: 2026-08-17

This is a graph-only merge revision for independently developed migrations.
"""

revision = "541_merge_staff_session_and_ticket_comment_heads"
down_revision = (
    "540_staff_session_party_ratchet",
    "540_ticket_comment_mentions",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
