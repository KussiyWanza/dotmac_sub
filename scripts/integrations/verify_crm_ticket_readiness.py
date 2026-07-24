"""Fail deployment when enabled CRM ticket pull has no executable binding/job."""

from __future__ import annotations

import json

from app.services.db_session_adapter import db_session_adapter
from app.services.integrations.crm_ticket_readiness import (
    resolve_crm_ticket_pull_readiness,
)


def main() -> int:
    with db_session_adapter.read_session() as db:
        readiness = resolve_crm_ticket_pull_readiness(db)
        payload = readiness.to_dict()
    print(json.dumps(payload, sort_keys=True))
    return 0 if readiness.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
