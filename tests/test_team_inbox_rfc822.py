from __future__ import annotations

from textwrap import dedent
from uuid import UUID

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import InboxMessage, TeamInboxEmailRoute
from app.services import team_inbox_rfc822, team_inbox_smtp_inbound


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _route(db_session, team: ServiceTeam, email: str) -> None:
    db_session.add(
        TeamInboxEmailRoute(
            service_team_id=team.id,
            email_address=email.lower(),
            is_active=True,
        )
    )
    db_session.flush()


def test_parse_rfc822_email_decodes_headers_and_addresses():
    raw = dedent(
        """\
        From: =?utf-8?q?Ada_Nwosu?= <customer@example.com>
        To: Support <support@dotmac.io>, Billing <billing@dotmac.io>
        Cc: Field <field@dotmac.io>
        Subject: =?utf-8?q?Install_=26_invoice?=
        Message-ID: <msg-1@example.com>
        Date: Fri, 10 Jul 2026 09:00:00 +0100
        Content-Type: text/plain; charset=utf-8

        Please help with my install and invoice.
        """
    ).replace("\n", "\r\n")

    parsed = team_inbox_rfc822.parse_rfc822_email(
        raw.encode("utf-8"),
        source="smtp",
    )

    assert parsed.payload.from_address == "customer@example.com"
    assert parsed.payload.to_addresses == ["support@dotmac.io", "billing@dotmac.io"]
    assert parsed.payload.cc_addresses == ["field@dotmac.io"]
    assert parsed.payload.subject == "Install & invoice"
    assert parsed.payload.body == "Please help with my install and invoice."
    assert parsed.payload.message_id == "<msg-1@example.com>"
    assert parsed.payload.metadata["source"] == "smtp"
    assert parsed.payload.metadata["from_name"] == "Ada Nwosu"


def test_parse_rfc822_email_falls_back_to_envelope_recipients():
    raw = dedent(
        """\
        From: customer@example.com
        Subject: No To header
        Message-ID: <msg-2@example.com>
        Content-Type: text/plain; charset=utf-8

        Hello.
        """
    ).replace("\n", "\r\n")

    parsed = team_inbox_rfc822.parse_rfc822_email(
        raw.encode("utf-8"),
        rcpt_to=["support@dotmac.io"],
    )

    assert parsed.payload.to_addresses == ["support@dotmac.io"]
    assert parsed.payload.body == "Hello."


def test_html_only_email_has_readable_body_and_preserves_html():
    raw = dedent(
        """\
        From: customer@example.com
        To: support@dotmac.io
        Subject: HTML only
        Message-ID: <html-only@example.com>
        Content-Type: text/html; charset=utf-8

        <html><head><style>.hidden { display:none }</style><title>Ignore</title></head>
        <body><p>Hello &amp; welcome</p><div>Second line<br>after break</div>
        <ul><li>First</li><li>Second</li></ul><script>alert('no')</script></body></html>
        """
    ).replace("\n", "\r\n")

    parsed = team_inbox_rfc822.parse_rfc822_email(raw.encode())

    assert parsed.payload.body == (
        "Hello & welcome\n\nSecond line\nafter break\n\n- First\n- Second"
    )
    assert parsed.payload.metadata["body_text"] == parsed.payload.body
    assert parsed.payload.metadata["html_body"].startswith("<html>")
    assert "alert" not in parsed.payload.body


def test_multipart_email_prefers_text_and_preserves_html():
    raw = dedent(
        """\
        From: customer@example.com
        To: support@dotmac.io
        Subject: Alternative
        Message-ID: <alternative@example.com>
        MIME-Version: 1.0
        Content-Type: multipart/alternative; boundary="alt"

        --alt
        Content-Type: text/plain; charset=utf-8

        Preferred plain text.
        --alt
        Content-Type: text/html; charset=utf-8

        <p>Different <strong>HTML</strong> text.</p>
        --alt--
        """
    ).replace("\n", "\r\n")

    parsed = team_inbox_rfc822.parse_rfc822_email(raw.encode())

    assert parsed.payload.body == "Preferred plain text."
    assert parsed.payload.metadata["body_text"] == "Preferred plain text."
    assert "<strong>HTML</strong>" in parsed.payload.metadata["html_body"]


def test_smtp_intake_routes_and_stores_attachment_metadata(db_session):
    """Same coverage, now over the path production actually uses.

    ``receive_rfc822_email`` was a second ingestion route that bypassed the
    observation ledger entirely; it had no caller outside this file and has
    been removed. These two cases move onto ``handle_smtp_message``.
    """
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    _route(db_session, support, "support@dotmac.io")
    db_session.commit()
    raw = dedent(
        """\
        From: customer@example.com
        To: support@dotmac.io
        Subject: Attachment
        Message-ID: <msg-3@example.com>
        MIME-Version: 1.0
        Content-Type: multipart/mixed; boundary="boundary"

        --boundary
        Content-Type: text/plain; charset=utf-8

        See attached.
        --boundary
        Content-Type: text/plain; name="note.txt"
        Content-Disposition: attachment; filename="note.txt"

        hello attachment
        --boundary--
        """
    ).replace("\n", "\r\n")

    result = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="customer@example.com",
        rcpt_to=["support@dotmac.io"],
        data=raw.encode("utf-8"),
    )
    db_session.commit()

    message = db_session.get(InboxMessage, UUID(result.message_id))

    assert result.kind == "received"
    assert message.body == "See attached."
    assert message.metadata_["body_text"] == "See attached."
    assert message.metadata_["attachments"][0]["filename"] == "note.txt"
    assert message.metadata_["attachments"][0]["mime_type"] == "text/plain"
    assert message.conversation.primary_service_team_id == support.id


def test_smtp_intake_deduplicates_message_id(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    _route(db_session, support, "support@dotmac.io")
    db_session.commit()
    raw = dedent(
        """\
        From: customer@example.com
        To: support@dotmac.io
        Subject: Duplicate
        Message-ID: <msg-4@example.com>
        Content-Type: text/plain; charset=utf-8

        Hello.
        """
    ).replace("\n", "\r\n")

    first = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="customer@example.com",
        rcpt_to=["support@dotmac.io"],
        data=raw.encode("utf-8"),
    )
    db_session.commit()
    second = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="customer@example.com",
        rcpt_to=["support@dotmac.io"],
        data=raw.encode("utf-8"),
    )
    db_session.commit()

    assert first.kind == "received"
    assert second.kind == "duplicate"
    assert second.conversation_id == first.conversation_id
