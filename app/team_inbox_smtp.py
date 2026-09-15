"""Dedicated runtime and probes for team-inbox SMTP ingestion.

Run the listener as one supervised process::

    python -m app.team_inbox_smtp serve

The readiness probe checks the SMTP listener without creating inbox data. The
end-to-end probe deliberately creates one clearly marked inbound message and
verifies that the owner committed it to the inbox database.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import smtplib
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from typing import Any

from app.config import settings
from app.services import team_inbox_health, team_inbox_smtp_inbound

logger = logging.getLogger(__name__)

EXIT_RUNTIME_FAILURE = 1
EXIT_CONFIGURATION_ERROR = 2
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2525
DEFAULT_READINESS_TIMEOUT_SECONDS = 10.0
PROBE_HEADER_VALUE = team_inbox_smtp_inbound.SMTP_PROBE_HEADER_VALUE


def _configured_port() -> int:
    return settings.team_inbox_smtp_inbound_port


def _probe_recipient() -> str | None:
    return settings.team_inbox_smtp_probe_recipient or None


def smtp_readiness(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
) -> bool:
    """Return whether the listener accepts an SMTP ``NOOP``."""
    try:
        with smtplib.SMTP(host=host, port=port, timeout=timeout) as client:
            code, _message = client.noop()
        return 200 <= int(code) < 400
    except (OSError, smtplib.SMTPException):
        logger.warning(
            "team_inbox_smtp_readiness_failed host=%s port=%s",
            host,
            port,
            exc_info=True,
        )
        return False


def build_probe_message(*, sender: str, recipient: str) -> tuple[EmailMessage, str]:
    """Build one traceable probe message and return its provider-stable ID."""
    probe_id = str(uuid.uuid4())
    message_id = f"<dotmac-smtp-probe-{probe_id}@sub.local>"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "[Dotmac probe] Team inbox SMTP delivery"
    message["Message-ID"] = message_id
    message["X-Dotmac-Probe"] = PROBE_HEADER_VALUE
    message["X-Dotmac-Probe-ID"] = probe_id
    message.set_content(
        "Synthetic deployment probe. This message verifies SMTP-to-inbox delivery."
    )
    return message, probe_id


def send_probe_message(
    message: EmailMessage,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Submit a probe through the canonical outbound email transport.

    Delivery then returns through the environment's real inbound/MX relay path
    before the team-inbox owner verifies the exact generated Message-ID.
    """
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    from app.tasks.notifications import deliver_inbound_smtp_health_probe

    db = session_factory()
    try:
        return deliver_inbound_smtp_health_probe(
            db,
            recipient=str(message["To"]),
            message_id=str(message["Message-ID"]),
            marker=str(message["X-Dotmac-Probe"]),
            probe_id=str(message["X-Dotmac-Probe-ID"]),
        )
    finally:
        db.close()


def wait_for_probe_delivery(
    probe_id: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.25,
    session_factory: Callable[[], Any] | None = None,
) -> dict[str, str] | None:
    """Wait until the SMTP owner has committed the probe inbox message."""
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    deadline = time.monotonic() + timeout
    while True:
        db = session_factory()
        try:
            delivered = team_inbox_health.verify_smtp_probe_delivery(
                db,
                probe_id=probe_id,
            )
            if delivered is not None:
                return delivered
        finally:
            db.close()
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def run_e2e_probe(
    *,
    recipient: str | None,
    timeout: float,
    emit_result: bool = True,
) -> int:
    """Send one SMTP message and prove it reached the inbox database."""
    resolved_recipient = (recipient or _probe_recipient() or "").strip().lower()
    if not resolved_recipient:
        logger.error("team_inbox_smtp_probe_missing_recipient")
        return EXIT_CONFIGURATION_ERROR
    allowed = team_inbox_smtp_inbound.smtp_inbound_allowed_recipients()
    if resolved_recipient not in allowed:
        logger.error(
            "team_inbox_smtp_probe_recipient_not_allowed recipient=%s",
            resolved_recipient,
        )
        return EXIT_CONFIGURATION_ERROR

    message, probe_id = build_probe_message(
        sender="smtp-probe@observability.invalid",
        recipient=resolved_recipient,
    )
    try:
        submitted = send_probe_message(message)
    except Exception:
        logger.exception("team_inbox_smtp_probe_submit_failed")
        return EXIT_RUNTIME_FAILURE
    if not submitted:
        logger.error("team_inbox_smtp_probe_submit_failed")
        return EXIT_RUNTIME_FAILURE

    delivered = wait_for_probe_delivery(probe_id, timeout=timeout)
    if delivered is None:
        logger.error(
            "team_inbox_smtp_probe_delivery_timeout probe_id=%s",
            probe_id,
        )
        return EXIT_RUNTIME_FAILURE
    if emit_result:
        print(json.dumps({"status": "ok", **delivered}, sort_keys=True))
    else:
        logger.info(
            "team_inbox_smtp_continuous_probe_ok message_id=%s conversation_id=%s",
            delivered["message_id"],
            delivered["conversation_id"],
        )
    return 0


def serve_forever(
    *,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
    health_interval: float = 1.0,
) -> int:
    """Run one SMTP controller until SIGTERM/SIGINT or controller failure."""
    if not team_inbox_smtp_inbound.smtp_inbound_enabled():
        logger.error("team_inbox_smtp_runtime_disabled")
        return EXIT_CONFIGURATION_ERROR
    if not team_inbox_smtp_inbound.smtp_inbound_allowed_recipients():
        logger.error("team_inbox_smtp_runtime_missing_allowed_recipients")
        return EXIT_CONFIGURATION_ERROR

    shutdown = stop_event or threading.Event()
    if install_signal_handlers:

        def _request_shutdown(signum, _frame) -> None:
            logger.info("team_inbox_smtp_shutdown_requested signal=%s", signum)
            shutdown.set()

        signal.signal(signal.SIGTERM, _request_shutdown)
        signal.signal(signal.SIGINT, _request_shutdown)

    if not team_inbox_smtp_inbound.start_smtp_inbound_server():
        logger.error("team_inbox_smtp_runtime_start_failed")
        return EXIT_RUNTIME_FAILURE

    logger.info("team_inbox_smtp_runtime_ready")
    exit_code = 0
    probe_recipient = _probe_recipient()
    probe_interval = settings.team_inbox_smtp_probe_interval_seconds
    next_probe_at = time.monotonic() if probe_recipient else None
    try:
        while not shutdown.wait(health_interval):
            if not team_inbox_smtp_inbound.smtp_inbound_server_running():
                logger.error("team_inbox_smtp_runtime_controller_stopped")
                exit_code = EXIT_RUNTIME_FAILURE
                break
            if next_probe_at is not None and time.monotonic() >= next_probe_at:
                probe_result = run_e2e_probe(
                    recipient=probe_recipient,
                    timeout=float(settings.team_inbox_smtp_probe_timeout_seconds),
                    emit_result=False,
                )
                if probe_result != 0:
                    logger.error(
                        "team_inbox_smtp_continuous_probe_failed exit_code=%s",
                        probe_result,
                    )
                next_probe_at = time.monotonic() + probe_interval
    finally:
        team_inbox_smtp_inbound.stop_smtp_inbound_server()
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the supervised SMTP listener")

    readiness = subparsers.add_parser(
        "readiness", help="Check the listener with SMTP NOOP"
    )
    readiness.add_argument("--host", default=DEFAULT_HOST)
    readiness.add_argument("--port", type=int, default=_configured_port())
    readiness.add_argument(
        "--timeout", type=float, default=DEFAULT_READINESS_TIMEOUT_SECONDS
    )

    e2e = subparsers.add_parser(
        "e2e-probe", help="Send a synthetic message and verify its inbox row"
    )
    e2e.add_argument(
        "--timeout",
        type=float,
        default=float(settings.team_inbox_smtp_probe_timeout_seconds),
    )
    e2e.add_argument("--recipient", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=settings.team_inbox_smtp_log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "serve":
        return serve_forever()
    if args.command == "readiness":
        return (
            0
            if smtp_readiness(host=args.host, port=args.port, timeout=args.timeout)
            else EXIT_RUNTIME_FAILURE
        )
    return run_e2e_probe(
        recipient=args.recipient,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
