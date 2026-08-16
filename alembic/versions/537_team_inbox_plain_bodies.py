"""Backfill readable Team Inbox email bodies.

Revision ID: 537_team_inbox_plain_bodies
Revises: 536_integrator_ingress_scopes
Create Date: 2026-08-16
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op
from app.services.team_inbox_rfc822 import html_to_readable_text

revision = "537_team_inbox_plain_bodies"
down_revision = "536_integrator_ingress_scopes"
branch_labels = None
depends_on = None

_HTML_START = re.compile(
    r"^\s*(?:<!doctype\s+html\b|<html\b|<body\b|<p\b|<div\b)", re.I
)


def upgrade() -> None:
    messages = sa.table(
        "inbox_messages",
        sa.column("id", sa.Uuid()),
        sa.column("channel_type", sa.String()),
        sa.column("direction", sa.String()),
        sa.column("body", sa.Text()),
        sa.column("metadata", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            messages.c.id, messages.c.direction, messages.c.body, messages.c.metadata
        )
        .where(messages.c.channel_type == "email")
        .where(messages.c.body.is_not(None))
    )
    for row in rows:
        body = str(row.body or "")
        if not _HTML_START.match(body):
            continue
        metadata = dict(row.metadata or {})
        if row.direction == "outbound" and str(metadata.get("body_text") or "").strip():
            plain = str(metadata["body_text"]).strip()
        elif (
            row.direction == "inbound"
            and not str(metadata.get("body_text") or "").strip()
        ):
            plain = html_to_readable_text(body)
            if not plain:
                continue
            metadata.setdefault("html_body", body)
            metadata["body_text"] = plain
        else:
            continue
        connection.execute(
            messages.update()
            .where(messages.c.id == row.id)
            .values(body=plain, metadata=metadata)
        )


def downgrade() -> None:
    # The original inbound HTML remains in metadata.html_body. Restoring raw
    # markup to the primary thread body would reintroduce the production bug.
    pass
