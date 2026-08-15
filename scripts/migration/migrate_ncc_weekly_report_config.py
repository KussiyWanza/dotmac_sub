#!/usr/bin/env python3
"""Validate and optionally import the retired CRM NCC schedule into Selfcare.

The input is an operator-exported JSON object; this script never reaches into
CRM. It is dry-run-only unless ``--apply`` is supplied. Recipient addresses are
redacted from output.

Example::

    python scripts/migration/migrate_ncc_weekly_report_config.py crm-ncc.json
    python scripts/migration/migrate_ncc_weekly_report_config.py crm-ncc.json --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services import ncc_report_email  # noqa: E402
from app.services.db_session_adapter import db_session_adapter  # noqa: E402
from app.services.owner_commands import CommandContext  # noqa: E402


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("enabled must be a JSON boolean or a recognized boolean value")


def _command(payload: dict[str, object]) -> (
    ncc_report_email.UpdateNccWeeklyDeliveryConfigurationCommand
):
    return ncc_report_email.UpdateNccWeeklyDeliveryConfigurationCommand(
        context=CommandContext.system(
            actor="script:migrate-ncc-weekly-report-config",
            scope="ncc.weekly_delivery_configuration",
            reason="import validated CRM NCC weekly delivery configuration",
        ),
        enabled=_boolean(payload.get("enabled", False)),
        to_address=str(payload.get("to") or ""),
        cc_addresses=str(payload.get("cc") or ""),
        bcc_addresses=str(payload.get("bcc") or ""),
        sender_key=str(payload.get("sender_key") or ""),
        subject=str(payload.get("subject") or ""),
        body_template=str(payload.get("body_template") or ""),
        local_time=str(payload.get("local_time") or "08:00"),
        timezone=str(payload.get("timezone") or "Africa/Lagos"),
        send_day=str(payload.get("send_day") or "tuesday"),
        lookback_days=int(payload.get("lookback_days") or 7),
    )


def _redacted_preview(
    preview: ncc_report_email.NccWeeklyDeliveryConfigurationPreview,
) -> dict[str, object]:
    return {
        "primary_recipient_configured": preview.recipients.to is not None,
        "cc_recipient_count": len(preview.recipients.cc),
        "bcc_recipient_count": len(preview.recipients.bcc),
        "sender_key": preview.sender_key,
        "subject": preview.subject,
        "local_time": preview.local_time.strftime("%H:%M"),
        "timezone": preview.timezone,
        "send_day": preview.send_day.value,
        "lookback_days": preview.lookback_days,
        "body_template_sha256": hashlib.sha256(
            preview.body_template.encode()
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Operator-exported CRM JSON file")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist through the Selfcare typed owner (default is dry-run)",
    )
    args = parser.parse_args()
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("Configuration must be one JSON object.")
    command = _command(raw)
    preview = ncc_report_email.preview_configuration(command)
    result: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "enabled": command.enabled,
        "validated": _redacted_preview(preview),
    }
    if args.apply:
        with db_session_adapter.owner_command_session() as db:
            outcome = ncc_report_email.update_configuration(db=db, command=command)
        result["effective"] = {
            "enabled": outcome.configuration.enabled,
            "send_day": outcome.configuration.send_day.value,
            "local_time": outcome.configuration.local_time.strftime("%H:%M"),
            "timezone": outcome.configuration.timezone,
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
