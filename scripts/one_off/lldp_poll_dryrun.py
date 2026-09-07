"""Preview the LLDP topology poll against the live fleet — no DB writes.

Reads detached neighbor observations with no session held during network I/O.
Prints collection counts and candidate pairs; persistence is not simulated.

    python -m scripts.one_off.lldp_poll_dryrun

Run on a host with reachability to the MikroTik NAS fleet.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.db import SessionLocal
from app.services.topology.lldp_contracts import LldpReadQuery
from app.services.topology.lldp_poller import poll_all, read_snapshot


def main() -> int:
    db = SessionLocal()
    try:
        snapshot = read_snapshot(db, query=LldpReadQuery(datetime.now(UTC)))
    finally:
        try:
            db.rollback()
        finally:
            db.close()
    poll = poll_all(snapshot=snapshot)
    names = {device.id: device.name for device in snapshot.devices}
    print("LLDP collection preview (no writes; reconciliation not simulated)")
    print(json.dumps(poll.stats.to_dict(), indent=2))
    for edge in poll.edges:
        print(
            f"  {names[edge.source_device_id]} <-> {names[edge.target_device_id]} ({edge.medium.value})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
