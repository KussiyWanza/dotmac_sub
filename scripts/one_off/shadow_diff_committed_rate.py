"""Shadow-diff the committed-rate change before it touches a live session.

Adding ``rx-rate-min``/``tx-rate-min`` changes what every affected subscriber's
NAS is told. This reports, per subscription, the rate limit the current code
WOULD emit against the one live in the target's ``radreply`` — and writes
nothing at all.

It is the gate for the dedicated CIR cutover: run it, read the summary, and
only proceed when the changed set is exactly the offers you intended and the
unchanged set really is unchanged.

Usage:
    python -m scripts.one_off.shadow_diff_committed_rate
    python -m scripts.one_off.shadow_diff_committed_rate --show-unchanged
    python -m scripts.one_off.shadow_diff_committed_rate --family dedicated
"""

from __future__ import annotations

import argparse
from collections import Counter

from app.db import SessionLocal
from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.services.external_radius_targets import active_external_radius_targets
from app.services.radius_population import _effective_profile, _rate_limit

_ATTRIBUTE = "Mikrotik-Rate-Limit"


def _live_rate_limits(db) -> dict[str, str]:
    """username -> the Mikrotik-Rate-Limit currently in the live radreply.

    Read-only, and read from the same targets the projection writes to, so a
    difference here is a real difference rather than a config mismatch.

    A target's config carries its database password. Nothing in this function
    prints a target — only its name — because a diagnostic that dumps the
    config on an error path leaks the credential into whatever captured the
    output.
    """
    from sqlalchemy import create_engine, text

    live: dict[str, str] = {}
    for target in active_external_radius_targets(db, capability="users"):
        name = str(target.get("target_name") or target.get("target_id") or "?")
        db_url = target.get("db_url")
        reply_table = str(target.get("radreply_table") or "radreply")
        if not db_url:
            print(f"  ! target {name} has no db_url; skipped")
            continue
        engine = create_engine(db_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        f"SELECT username, value FROM {reply_table} "  # noqa: S608
                        "WHERE attribute = :attribute"
                    ),
                    {"attribute": _ATTRIBUTE},
                ).fetchall()
            for username, value in rows:
                live[str(username)] = str(value)
            print(f"  target {name}: {len(rows)} rows")
        except Exception as exc:  # noqa: BLE001 - diagnostic must not leak URL
            print(f"  ! target {name} unreadable: {type(exc).__name__}")
        finally:
            engine.dispose()
    return live


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-unchanged", action="store_true")
    parser.add_argument("--family", help="restrict to one plan family")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        print("reading live radreply ...")
        live = _live_rate_limits(db)
        print(f"  {len(live)} live {_ATTRIBUTE} rows\n")

        # The projection resolves a credential-level profile (FUP/dunning
        # throttle) ahead of the subscription profile, and a profile's stored
        # mikrotik_rate_limit WINS over anything derived from the offer.
        # Computing offer-derived values alone reports thousands of spurious
        # differences that the real sweep would never write.
        profiles_by_id = {
            profile.id: profile for profile in db.query(RadiusProfile).all()
        }

        query = (
            db.query(Subscription, CatalogOffer, AccessCredential)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .join(
                AccessCredential,
                AccessCredential.subscription_id == Subscription.id,
            )
            .filter(Subscription.status == SubscriptionStatus.active)
        )
        if args.family:
            query = query.filter(CatalogOffer.plan_family == args.family)

        counts: Counter[str] = Counter()
        changes: list[tuple[str, str, str, str]] = []

        for subscription, offer, credential in query.all():
            username = str(credential.username or "").strip()
            if not username:
                counts["no_username"] += 1
                continue
            effective_profile = _effective_profile(
                credential, subscription.radius_profile, profiles_by_id
            )
            computed = _rate_limit(offer, effective_profile)
            current = live.get(username)
            if current is None:
                counts["absent_from_radreply"] += 1
                continue
            if computed == current:
                counts["unchanged"] += 1
                if args.show_unchanged:
                    print(f"  = {username:28} {current}")
                continue
            counts["changed"] += 1
            changes.append((username, offer.name or "?", current, computed or "(none)"))

        if changes:
            print(f"CHANGED ({len(changes)}):\n")
            for username, offer_name, current, computed in sorted(changes):
                print(f"  {username}  [{offer_name}]")
                print(f"      live: {current}")
                print(f"      new : {computed}\n")

        print("summary:")
        for key in (
            "changed",
            "unchanged",
            "absent_from_radreply",
            "no_username",
        ):
            print(f"  {key:22} {counts[key]}")
        print("\nNothing was written.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
