"""Which ONTs could safely be in the first bounded reconcile cohort?

The global sweep has been contained since 2026-08-02. Re-enabling it fleet-wide
is not a restoration -- 701 of 718 candidates have never been reconciled at all,
so it would be a first-ever run at scale. The safe shape is one small named
cohort, converged and verified, then grown.

This answers the prior question: is there anything to put in cohort 1? It
intersects every independent gate a device must clear, and reports how many
survive each one, so an empty answer says *which* gate emptied it.

The gates, each already established elsewhere:

  * **in the sweep population** -- `sweep_candidates`, the same function
    `run_sweep_once` uses, so this cannot report on a different set of devices
    than the one that would actually be swept;
  * **previously observed** -- an ONT with no observation row has no stored
    state, so what the sweep would do to it cannot be known without contacting
    the device;
  * **no sentinel rule fires** -- from `reconcile.sentinels`; a device where an
    unset placeholder would be delivered is not a safe first candidate, whether
    that write is currently guarded or merely undeclared;
  * **canonical PON identity** -- from `network.pon_port_identity`; a device
    hanging off a port nobody can structurally name should not be converged
    while the identity census is dirty;
  * **not held** -- a reviewed hold is a deliberate exclusion;
  * **reachable recently** -- a device with a non-zero consecutive-unreachable
    counter is not a candidate for proving convergence works.

Read-only: REPEATABLE READ READ ONLY, no device I/O, session rolled back.

Exit 0 if at least one ONT clears every gate; 1 if the intersection is empty,
because that is a finding, not a failure -- it means cohort 1 has to be
constructed by hand rather than selected by a predicate.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models.network import OntUnit, PonPort
from app.models.ont_observation import OntObservation
from app.services.network.ont_reconcile_eligibility import held_ont_ids
from app.services.network.pon_port_identity import classify
from app.services.network.reconcile.adapters import desired_from_ont_unit
from app.services.network.reconcile.sentinels import RULES
from app.services.network.reconcile.sweeper import sweep_candidates

EXIT_HAS_COHORT = 0
EXIT_EMPTY = 1


def main() -> int:
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )

        candidates = sweep_candidates(db)
        population = [c.ont_id for c in candidates]
        observed_ids = {
            row[0]
            for row in db.execute(
                select(OntObservation.ont_unit_id).where(
                    OntObservation.ont_unit_id.in_(population)
                )
            ).all()
        }
        held = set(held_ont_ids(db))

        # Gates are evaluated independently so an empty result names its cause
        # rather than just being empty.
        survivors: list[str] = []
        dropped: Counter[str] = Counter()
        pon_class_counts: Counter[str] = Counter()
        # Which rules block the devices that clear every other gate. If the
        # intersection is empty this is the list that has to be decided.
        blocking_rules: Counter[str] = Counter()

        for ont_id in population:
            if ont_id not in observed_ids:
                dropped["never_observed"] += 1
                continue
            if ont_id in held:
                dropped["held"] += 1
                continue
            ont = db.get(OntUnit, ont_id)
            if ont is None:
                dropped["missing_row"] += 1
                continue
            if (ont.consecutive_sweep_unreachable or 0) > 0:
                dropped["unreachable_streak"] += 1
                continue

            if ont.pon_port_id is None:
                dropped["no_pon_port"] += 1
                continue
            pon = db.get(PonPort, ont.pon_port_id)
            if pon is None:
                dropped["pon_port_missing"] += 1
                continue
            pon_class = classify(db, pon)
            pon_class_counts[pon_class] += 1
            if pon_class != "canonical":
                dropped["pon_identity_not_canonical"] += 1
                continue

            try:
                desired = desired_from_ont_unit(db, ont)
            except Exception as exc:  # noqa: BLE001
                dropped[f"desired_unreadable:{type(exc).__name__}"] += 1
                continue
            values = {
                r.source_key: getattr(desired, r.field, None)
                for r in RULES
                if r.source_key
            }
            firing = [
                r.field
                for r in RULES
                if r.measurable and r.fires_for(values, frozenset())
            ]
            if firing:
                dropped["sentinel_rule_fires"] += 1
                for f in firing:
                    blocking_rules[f] += 1
                continue

            survivors.append(str(ont_id))

        db.rollback()

    report = {
        "sweep_population": len(population),
        "cohort_eligible": len(survivors),
        "dropped_by_gate": dict(dropped.most_common()),
        "pon_identity_of_observed_unheld_reachable": dict(
            pon_class_counts.most_common()
        ),
        "rules_blocking_otherwise_eligible_onts": dict(blocking_rules.most_common()),
        "eligible_ont_ids": survivors[:50],
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    if not survivors:
        print(
            "\nNo ONT clears every gate. Cohort 1 must be constructed by hand: "
            "pick devices, repair their PON identity, and reconcile them once "
            "under review -- a predicate cannot select from an empty set.",
            file=sys.stderr,
        )
        return EXIT_EMPTY
    return EXIT_HAS_COHORT


if __name__ == "__main__":
    raise SystemExit(main())
