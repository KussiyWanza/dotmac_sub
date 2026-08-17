from __future__ import annotations

import base64
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from app.services import team_inbox_receive

_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_IGNORED_TAGS = {"head", "script", "style", "title"}


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self.ignored_depth += 1
        elif not self.ignored_depth:
            if tag in _BLOCK_TAGS or tag == "br":
                self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in _IGNORED_TAGS:
            self.ignored_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif not self.ignored_depth and (tag in _BLOCK_TAGS or tag == "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def html_to_readable_text(value: str | None) -> str:
    """Convert an email HTML document to safe, readable thread text."""
    if not value:
        return ""
    parser = _ReadableHTMLParser()
    parser.feed(value)
    parser.close()
    text = (
        html.unescape("".join(parser.parts)).replace("\r\n", "\n").replace("\r", "\n")
    )
    lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@dataclass(frozen=True)
class ParsedInboundEmail:
    payload: team_inbox_receive.InboundEmailPayload
    attachments: list[dict[str, Any]] = field(default_factory=list)


def decode_header_value(value: str | None) -> str | None:
    if not value:
        return None
    decoded = ""
    for fragment, encoding in decode_header(value):
        if isinstance(fragment, bytes):
            decoded += fragment.decode(encoding or "utf-8", errors="replace")
        else:
            decoded += fragment
    return decoded.strip() or None


def parse_address_headers(values: Iterable[str]) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for _name, address in getaddresses(values):
        normalized = address.strip()
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            addresses.append(normalized)
    return addresses


def _payload_bytes(value: object | None) -> bytes:
    if isinstance(value, bytes):
        return value
    if value is None:
        return b""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return str(value).encode("utf-8", errors="replace")


def extract_bodies(message: Message) -> tuple[str | None, str | None]:
    text_body = None
    html_body = None
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            payload = _payload_bytes(part.get_payload(decode=True))
            charset = part.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
            if content_type == "text/plain" and text_body is None:
                text_body = content
            elif content_type == "text/html" and html_body is None:
                html_body = content
    else:
        payload = _payload_bytes(message.get_payload(decode=True))
        charset = message.get_content_charset() or "utf-8"
        content = payload.decode(charset, errors="replace")
        if message.get_content_type() == "text/html":
            html_body = content
        else:
            text_body = content
    return text_body, html_body


def extract_attachments(message: Message) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        content_id = part.get("Content-ID")
        if "attachment" not in disposition and not filename and not content_id:
            continue
        payload = _payload_bytes(part.get_payload(decode=True))
        if not payload:
            continue
        attachments.append(
            {
                "file_name": decode_header_value(filename) if filename else None,
                "mime_type": part.get_content_type(),
                "file_size": len(payload),
                "content_id": content_id,
                "content_base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    return attachments


# Retained verbatim, interpreted nowhere. Admission policy for inbound email
# is undecided, but the evidence it will need is only present at ingestion:
# nothing can recover an SPF or DKIM result for a message already accepted.
# Deferring the decision is fine; deferring capture would make every message
# received in the meantime permanently un-adjudicable.
_AUTHENTICATION_HEADERS = (
    "Authentication-Results",
    "ARC-Authentication-Results",
    "Received-SPF",
    "DKIM-Signature",
    "ARC-Seal",
)
_MAX_RECEIVED_HOPS = 12


def _authentication_headers(message: Message) -> dict[str, Any]:
    """Transport-authentication evidence, stored raw for a later policy.

    Kept as the provider wrote it rather than parsed into a verdict: a verdict
    embeds an interpretation, and which interpretation is correct is exactly
    what has not been decided.
    """
    captured: dict[str, Any] = {}
    for header in _AUTHENTICATION_HEADERS:
        values = [str(value) for value in message.get_all(header, []) if value]
        if values:
            captured[header.lower()] = values
    # The relay chain is what tells you where a claim entered our perimeter.
    received = [str(value) for value in message.get_all("Received", []) if value]
    if received:
        captured["received"] = received[:_MAX_RECEIVED_HOPS]
    return captured


def _parse_received_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_rfc822_email(
    data: bytes,
    *,
    mail_from: str | None = None,
    rcpt_to: list[str] | None = None,
    source: str = "rfc822",
    fallback_service_team_id: str | None = None,
) -> ParsedInboundEmail:
    message = message_from_bytes(data, policy=policy.default)
    from_name, from_address = parseaddr(message.get("From") or "")
    if not from_address:
        from_address = (mail_from or "").strip()
    to_addresses = parse_address_headers(message.get_all("To", []))
    if not to_addresses and rcpt_to:
        to_addresses = parse_address_headers(rcpt_to)
    cc_addresses = parse_address_headers(message.get_all("Cc", []))
    subject = decode_header_value(message.get("Subject"))
    text_body, html_body = extract_bodies(message)
    body_text = (text_body or "").strip() or html_to_readable_text(html_body)
    body = body_text or subject or "(no content)"
    received_at = _parse_received_at(message.get("Date"))
    metadata: dict[str, Any] = {
        "source": source,
        "from_raw": message.get("From"),
        "from_name": decode_header_value(from_name) if from_name else None,
        "to_raw": message.get("To"),
        "cc_raw": message.get("Cc"),
        "reply_to": parse_address_headers(message.get_all("Reply-To", [])),
        "recipients": list(rcpt_to or []),
    }
    authentication = _authentication_headers(message)
    if authentication:
        metadata["authentication"] = authentication
    smtp_probe = decode_header_value(message.get("X-Dotmac-Probe"))
    if smtp_probe:
        metadata["smtp_probe"] = smtp_probe
    if html_body:
        metadata["html_body"] = html_body
    metadata["body_text"] = body

    return ParsedInboundEmail(
        payload=team_inbox_receive.InboundEmailPayload(
            from_address=from_address,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            subject=subject,
            body=body,
            message_id=message.get("Message-ID"),
            in_reply_to=message.get("In-Reply-To"),
            references=message.get("References"),
            received_at=received_at,
            fallback_service_team_id=fallback_service_team_id,
            metadata=metadata,
        ),
        attachments=extract_attachments(message),
    )


# `receive_rfc822_email` used to live here: a second way into the inbox that
# skipped the observation ledger, inlined base64 attachment bytes into message
# metadata, and looked a message up by a string primary key. It had no caller
# outside its own tests. SMTP intake now has one path —
# `team_inbox_smtp_inbound.handle_smtp_message` records the observation first
# and `team_inbox_processing` resolves it — so what arrived is always durable
# before anything is derived from it.
