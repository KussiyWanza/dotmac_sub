"""Count how many live ONTs would receive an unset-sentinel write.

The gate on re-enabling ``network.ont_reconcile``. Three layers on the ONT
desired-state path substitute defaults for missing configuration, and the
planner cannot tell those placeholders from operator intent — so it writes
them. ``app.services.network.reconcile.sentinels`` names every substitution;
this script turns that registry into a number against live data.

Results are grouped by execution authority:

  * ``inadmissible`` — already guarded in the planner and applier. The count is
    what those guards now suppress, i.e. what would have been delivered.
  * ``delegated`` — refused by a different named owner, so not guarded here.
  * ``undeclared`` — still executes with nothing behind it, and every one is on
    the shrink-only authority-debt baseline. These counts are what the pending
    declarations are meant to be decided against, and they are the difference
    between "the sweep is safe to re-enable" and "it is not".

A rule whose input does not live in stored configuration (an ACS server row, an
observed device value) is reported as **unmeasured**, never as zero affected.
A fake zero is the failure mode this whole exercise exists to prevent.

The population comes from ``reconcile.sweeper.sweep_candidates`` — the same
function ``run_sweep_once`` uses — so the risk profile reported here is always
for the exact set of devices that will actually be swept, including its
``NULLS FIRST`` ordering. Each rule is also counted against the never-reconciled
subset, because those ONTs are processed first and are the ones most likely to
hold sparse desired config.

Read-only: opens no writes and performs no device I/O. Safe to run against
production.

Exit codes: ``0`` clean; ``1`` when any ONT's effective configuration could not
be read (an unread ONT is an uncounted ONT, so the total is not a gate); ``2``
when ``--limit`` was used, because a sample cannot clear a release gate.

Run from the repo root as a module::

    poetry run python -m scripts.network.ont_sentinel_blast_radius [--json] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

from app.db import SessionLocal
from app.models.network import OntUnit
from app.services.control_plane_intent import DesiredValueAuthority
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.reconcile.sentinels import RULES, SentinelRule
from app.services.network.reconcile.sweeper import sweep_candidates

EXIT_OK = 0
EXIT_UNREADABLE = 1
EXIT_SAMPLED = 2


@dataclass
class RuleCount:
    rule: SentinelRule
    affected: int = 0
    affected_never_reconciled: int = 0

    def as_dict(self) -> dict[str, object]:
        measured = self.rule.measurable
        return {
            "field": self.rule.field,
            "layer": self.rule.layer,
            "source": self.rule.config_path or self.rule.source_key,
            "authority": self.rule.authority.value,
            "adjudication": self.rule.adjudication.value,
            "declared_by": self.rule.declared_by,
            "writes": self.rule.writes,
            "measured": measured,
            "affected": self.affected if measured else None,
            "affected_never_reconciled": (
                self.affected_never_reconciled if measured else None
            ),
        }


@dataclass
class Report:
    population: int = 0
    never_reconciled: int = 0
    sampled: bool = False
    unreadable: list[str] = field(default_factory=list)
    rules: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "population": self.population,
            "never_reconciled": self.never_reconciled,
            "sampled": self.sampled,
            "unreadable_count": len(self.unreadable),
            "unreadable": self.unreadable,
            "rules": self.rules,
            "gate_usable": not self.sampled and not self.unreadable,
        }


def scan(limit: int | None = None) -> Report:
    counts = {rule.field: RuleCount(rule) for rule in RULES}
    measurable = [entry for entry in counts.values() if entry.rule.measurable]
    report = Report(sampled=limit is not None)

    with SessionLocal() as db:
        for candidate in sweep_candidates(db, max_onts=limit):
            ont = db.get(OntUnit, candidate.ont_id)
            if ont is None:  # pragma: no cover - row vanished mid-scan
                continue
            report.population += 1
            if candidate.never_reconciled:
                report.never_reconciled += 1
            try:
                effective = resolve_effective_ont_config(db, ont)
            except Exception as exc:  # noqa: BLE001 - report, never abort the scan
                report.unreadable.append(f"{ont.serial_number}: {exc}")
                continue
            values = effective.get("values") or {}
            config_keys = frozenset(effective.get("desired_config_keys") or ())
            for entry in measurable:
                if not entry.rule.fires_for(values, config_keys):
                    continue
                entry.affected += 1
                if candidate.never_reconciled:
                    entry.affected_never_reconciled += 1

    report.rules = [entry.as_dict() for entry in counts.values()]
    return report


def _print_report(report: Report) -> None:
    print(f"ONT sweep population: {report.population}")
    print(f"  never reconciled (swept first): {report.never_reconciled}")
    if report.unreadable:
        print(f"  UNREADABLE effective config: {len(report.unreadable)}")
        for line in report.unreadable[:10]:
            print(f"    {line}")
        print("  An unread ONT is an uncounted ONT — this total is not a gate.")
    if report.sampled:
        print("  SAMPLED (--limit): counts are a sample, not a release gate.")

    for authority, heading in (
        (
            DesiredValueAuthority.inadmissible,
            "GUARDED (suppressed by the planner/applier guards)",
        ),
        (
            DesiredValueAuthority.delegated,
            "DELEGATED (refused by another named owner)",
        ),
        (
            DesiredValueAuthority.undeclared,
            "AUTHORITY DEBT (executes with no declaration — decide before re-enabling)",
        ),
        (
            DesiredValueAuthority.declared_default,
            "DECLARED (an owner approved this default)",
        ),
    ):
        rows = [row for row in report.rules if row["authority"] == authority.value]
        if not rows:
            continue
        print(f"\n{heading}")
        measured = [row for row in rows if row["measured"]]
        unmeasured = [row for row in rows if not row["measured"]]
        for row in sorted(measured, key=lambda r: -int(r["affected"])):
            print(
                f"  {row['field']:<30} {row['affected']:>6}"
                f"  (never-reconciled {row['affected_never_reconciled']:>5})"
                f"  [{row['layer']}] ← {row['source']}"
            )
            print(f"  {'':<30} writes {row['writes']}")
        for row in unmeasured:
            print(
                f"  {row['field']:<30} {'UNMEASURED':>6}"
                f"  [{row['layer']}] input is not stored config"
            )
            print(f"  {'':<30} writes {row['writes']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit the raw report")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="scan only the first N ONTs in sweep order — a sample, not a gate",
    )
    args = parser.parse_args(argv)

    report = scan(limit=args.limit)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        _print_report(report)

    if report.unreadable:
        return EXIT_UNREADABLE
    if report.sampled:
        return EXIT_SAMPLED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
