#!/usr/bin/env python3
"""Report whether the Integrator reproduces what Sub's own receiver recorded.

Shadow evidence for repointing a provider callback at the Integrator. Read-only
and PII-free: the output carries verdict counts, blocking reason codes and
disagreeing FIELD names only, so it can be run against a production-derived
restore and pasted into a review unredacted.

Input is a JSON file of Integrator envelopes — one object, or an array of them —
exactly as the Integrator would POST to
``/api/v1/integration/observations/{capability_binding_id}``. Capture them from
the Integrator's own outbox during the shadow window; nothing here talks to the
Integrator, so this can be run on a host with no route to it.

Exits non-zero while any blocking reason remains, and also on an EMPTY
population: an empty comparison proves the producers agree exactly as much as
running no comparison at all. ``--report-only`` surveys without gating.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from app.db import read_only_snapshot_session
from app.schemas.integrator_observation import IntegratorObservationEnvelope
from app.services.team_inbox_integrator_mirror import compare_population


def _load(path: Path) -> tuple[IntegratorObservationEnvelope, ...]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    items = decoded if isinstance(decoded, list) else [decoded]
    try:
        return tuple(
            IntegratorObservationEnvelope.model_validate(item) for item in items
        )
    except ValidationError as exc:
        # A malformed capture is refused rather than silently narrowed to the
        # envelopes that happened to parse: a report over a subset would
        # understate the disagreement it exists to measure.
        raise SystemExit(f"invalid envelope capture in {path}: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "envelopes",
        type=Path,
        help="JSON file holding one Integrator envelope, or an array of them",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even while blocking reasons remain",
    )
    args = parser.parse_args(argv)

    envelopes = _load(args.envelopes)
    # One snapshot, and structurally unable to write. A parity claim assembled
    # from many statements has to see one snapshot, or a webhook delivery
    # landing mid-run can make two envelopes in the same report disagree about
    # the same population — and a report that measures production must not be
    # able to change it.
    with read_only_snapshot_session() as db:
        report = compare_population(db, envelopes=envelopes)

    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.is_cutover_safe and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
