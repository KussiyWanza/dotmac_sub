# Plan family architecture

Normative design for the commercial plan families and how each is expressed,
enforced and verified. Owner: catalog + RADIUS provisioning.

Companion to `SOT_RELATIONSHIP_MAP.md`. Where this document and the executable
registry in `app/services/sot_relationships.py` disagree, report the conflict —
do not guess.

## 1. The families

Two mechanisms bound what a customer gets, and **each family uses exactly one
of them**. Confusing the two is what produced a catalogue where the family
documented as throttling had no rules and the family documented as never
throttling had sixteen.

**FUP** bounds *volume*: a **daily** bucket, warned then throttled, with the
overnight window free. **Contention** bounds *rate under load*: capacity shared
at a target ratio, with no volume limit at all.

| | `home_flex` | `high_speed_data` | `unlimited` | `dedicated` | `ip_block` |
|---|---|---|---|---|---|
| Bounded by | **FUP** | **FUP** | **Contention** | **CIR** | n/a |
| Data volume | Allowance | Allowance | Unmetered | Unmetered | n/a |
| On exhaustion | warn → throttle → block | warn → throttle → block | n/a | n/a | n/a |
| Contention | shared | shared | **1:5 target** | **1:1** | n/a |
| Rate under load | best effort | best effort | best effort | **guaranteed** | n/a |
| Public IP | No | No | No | **Yes** | **is the product** |
| Sold by | Mbps | **gigabytes** | Mbps | Mbps | block size |

The defining sentence for each:

- **home_flex** — cheap entry, capped. A volume allowance; past it you keep
  working at a reduced speed rather than losing service.
- **high_speed_data** — a burst speed with a volume bucket. Sold by the
  gigabyte, which is why dividing its price by `speed_download_mbps` produces
  a meaningless rate.
- **unlimited** — no volume limit and **no throttle, ever**. Speed is best
  effort at a 1:5 planning target; the tier rate is a ceiling, not a floor.
- **dedicated** — the tier rate is a floor as well as a ceiling, reserved 1:1,
  with a public IP and SLA credits. No FUP.
- **ip_block** — a routed block sold as a service in its own right, not an
  attachment to one.

### unlimited warns, it does not throttle

`unlimited` carries FUP rules, and every one of them is `action = notify` —
fair-use warnings scaled to the tier (200 GB entry, 500 GB mid, 1000–2000 GB
top) with no `speed_reduction_percent`. They tell a heavy user they are heavy.
They must never become `reduce_speed`: the customer-facing catalogue states
"we never throttle you for using it", and that claim is only true while this
holds.

`unlimited` therefore makes **no contention commitment** to the customer
either. `CatalogOffer.aggregation` on an unlimited offer is an internal
capacity-planning target, not a promise, and must not be published as one.

### The FUP ladder

Every FUP family uses the same shape, and it must stay uniform:

| Stage | Threshold | Period | Action |
|---|---|---|---|
| Warn | 80% of the daily bucket | `daily` | `notify` |
| Throttle | 100% | `daily` | `reduce_speed` **to 50% of the plan's own rate** |
| Free night | 22:00–05:00 subscriber-local | — | throttle lifts; traffic does not accrue |

Three properties of this shape are load-bearing and none may be traded away
for a simpler implementation:

1. **The bucket is daily, not monthly.** A monthly bucket lets a subscriber
   exhaust the month in three days and spend twenty-seven throttled; a daily
   bucket is self-healing and needs no `block` stage at all.
2. **The throttle is relative to the plan, not absolute.** 50% of a 50 Mbps
   Premium is 25 Mbps; 50% of a 6 Mbps Starter is 3 Mbps. A single flat
   throttle rate makes the expensive tier's penalty far harsher than the cheap
   tier's, which inverts what the customer paid for.
3. **The night window both lifts the throttle and stops accrual.** Half of it
   is not the feature: a customer still throttled at 22:00, or one whose
   overnight download eats tomorrow's bucket, has not been given a free night.

There is no `block` stage. Blocking a residential customer who paid for the
month is a billing conversation, not a traffic-management one.

### FUP has exactly one owner

**`fup_policies` + `fup_rules` owns every FUP decision** — thresholds, periods,
actions, throttle depth, and the night window. Nothing else may decide any of
them.

`usage_allowances` is **not** a second FUP authority and must not be read as
one. It owns a different decision: **billing**. `included_gb`, `overage_rate`
and `overage_cap_gb` are prorated into the `QuotaBucket` and turn into overage
charges (`app/services/usage.py::_prorate_allowance`). That is rating, not
enforcement, and the two are allowed to hold different numbers for good
commercial reasons.

The one genuine violation is **`usage_allowances.throttle_rate_mbps`**, which
states an enforcement decision inside a billing object. It is retired: see §12.

## 2. Field mapping

The schema already models all of this. Nothing new is required.

| Concept | Field | home_flex | high_speed_data | unlimited | dedicated |
|---|---|---|---|---|---|
| Volume bound | `fup_policies` + `fup_rules` | **ladder** | **ladder** | `notify` only | none |
| Bucket period | `fup_rules.consumption_period` | **daily** | **daily** | monthly | — |
| Post-FUP speed | `fup_rules.speed_reduction_percent` | **50** | **50** | — | — |
| Free night | `fup_rules.time_start` / `time_end` | **22:00–05:00** | **22:00–05:00** | — | — |
| Rate floor | `guaranteed_speed` | `none` | `none` | `none` | **`fixed`** |
| Floor value | `guaranteed_speed_limit_at` | NULL | NULL | NULL | **= line rate** |
| Contention target | `aggregation` | shared | shared | **5** | **1** |
| Public IP | `ip_block` offer | — | — | — | **bundled** |
| Service credits | `sla_profile_id` | NULL | NULL | NULL | **set** |
| Billing behaviour | `policy_set_id` | set | set | set | set |

`sla_profiles` is currently empty; a dedicated SLA profile must be created
before dedicated offers can reference one.

`usage_allowances` (`included_gb`, `throttle_rate_mbps`) is deliberately absent
from this table. It is a Splynx import artefact expressing the same intent as
the FUP engine in a weaker form, and reading it as authority produces wrong
answers — a `1000GB` plan with a blank `throttle_rate_mbps` still throttles,
because its policy carries a `reduce_speed` rule.

## 3. Enforcement — the RADIUS contract

`app/services/radius_population.py` emits `Mikrotik-Rate-Limit`. The RouterOS
attribute grammar is:

```
rx-rate/tx-rate [burst-rx/burst-tx [burst-threshold-rx/tx [burst-time-rx/tx
    [priority [rx-rate-min/tx-rate-min]]]]]
```

Today `_rate_limit()` emits only `{down}M/{up}M` — MIR, no committed rate. The
trailing `rx-rate-min/tx-rate-min` field is the CIR and is what distinguishes a
guarantee from best effort.

Target shapes:

```
home_flex   50M/50M                          # then profile swap on FUP
unlimited   50M/50M                          # MIR only — best effort
dedicated   50M/50M 0/0 0/0 0/0 8 50M/50M    # rx-rate-min = rx-rate → 1:1
```

**`guaranteed_speed = fixed` must be the only thing that adds the min field.**
One canonical writer: `_rate_limit()` derives it from the offer, and no caller
hand-builds a rate-limit string.

### FUP throttle path (home_flex)

The mechanism exists and is verified working:

1. Usage crosses `UsageAllowance.included_gb`.
2. The owning service sets `AccessCredential.radius_profile_id` to the throttle
   profile (`FUP Throttle 1Mbps` or a rate matching `throttle_rate_mbps`).
3. `_effective_profile()` gives the credential-level override precedence over
   the subscription profile — deliberately, so the authoritative `populate()`
   sweep cannot silently revert the throttle (SP-2).
4. Restoring service clears the override.

`unlimited` and `dedicated` must never acquire a FUP override. That is the
invariant which makes "no throttle, ever" true rather than aspirational.

## 4. Contention is a network property, not a catalog field

A ratio written on an offer commits nothing. The binding constraints sit
upstream and must be measured, not asserted:

- **PON split.** GPON is 2.488 G down / 1.244 G up shared across the split. At
  1:64, 64 × 100 Mbps sold is 6.4 G against 2.5 G — 1:2.6 at the PON alone.
  Symmetrical high tiers hit the 1.244 G upstream first. High tiers must carry a
  PON-fill rule, not just a catalog ratio.
- **BNG/NAS.** PPPoE termination plus per-subscriber queues. MikroTik HTB/PCQ
  does not spread across cores, so queue count drives single-CPU load and binds
  before uplink capacity does.
- **Transit/IXP.** Usually the commercial ceiling.

Therefore:

- `aggregation` is a **planning target per aggregation domain**, owned by
  capacity planning.
- A reconciler must compare sold capacity against provisioned capacity per PON
  and per BNG and raise drift. Until that exists, the number is decorative.
- **Dedicated is the only family whose ratio is a contractual promise**, and it
  is enforced by the CIR, not by the integer.

## 5. Invariants

Enforce in the catalog service, with architecture tests:

1. `plan_family = 'dedicated'` ⟹ `aggregation = 1` AND
   `guaranteed_speed = 'fixed'` AND `guaranteed_speed_limit_at` = line rate AND
   a bundled public-IP add-on.
2. `plan_family = 'unlimited'` ⟹ `usage_allowance_id IS NULL` AND
   `guaranteed_speed = 'none'`. An unlimited offer with an allowance is a
   contradiction in terms.
3. `plan_family = 'home_flex'` ⟹ `usage_allowance_id IS NOT NULL` AND the
   referenced allowance has `throttle_rate_mbps` set.
4. Within a family, a faster tier must never price at or below a slower one.
5. `aggregation` is uniform within a family (dedicated 1, others per policy).
6. No offer may be `show_on_customer_portal` while `code LIKE 'custom-%'`.

## 6. Current state and gap

As at 2026-08-06.

**FUP runs, but it does not implement §1.** `fup_policies`/`fup_rules` carry 39
active rules and 5 subscribers sit in an enforced state, all on
`high_speed_data`. `unlimited`'s sixteen rules are all `notify`, as required,
and `dedicated`'s policies correctly carry no rules. Everything else diverges:

| §1 requires | Production does |
|---|---|
| `daily` bucket | **`monthly` on all 39 rules.** No daily rule exists. |
| throttle to 50% of the plan rate | **flat 1 Mbps for everyone.** See below. |
| free night 22:00–05:00 | **nothing.** See below. |
| no `block` stage | `block` at 120% on six offers |

**The throttle is not proportional and `speed_reduction_percent` is
decorative.** Every throttled subscriber is moved to the single RADIUS profile
named by the `usage.fup_throttle_radius_profile_id` setting — today
*FUP Throttle 1Mbps* (`1024k/1024k`). `speed_reduction_percent` is written to
the rule (90 on every throttle rule) and copied onto `fup_states`, but it is
never read arithmetically anywhere in the tree. A 6 Mbps Starter and a 50 Mbps
Premium are both cut to 1 Mbps — an 83% cut for one and a 98% cut for the
other, from a field that says 90% for both.

**There is no free night.** One rule is *named* `Night free (00:00-06:00)`, on
the 1000GB offer, but it is `action = notify` with a threshold of `999999gb`,
so it can never fire. It is a label, not a mechanism. Two engine gaps stand
behind it, both of which must be closed before any night window works:

- *Night traffic still accrues.* `usage_summary.windowed_used_bytes` integrates
  the entire daily window. `fup_policies.traffic_accounting_start/end` is never
  applied to measurement — it is only reused as a fallback gate on rule
  *triggering* (`app/services/fup.py:764`).
- *A throttle never lifts early.* The only release path is
  `state.cap_resets_at` (`app/services/fup_enforcement.py:291`); there is no
  "nothing triggered → lift" branch. A rule window ending at 22:00 stops the
  rule re-firing but leaves the subscriber throttled until local midnight.
- *Window times are compared in UTC.* `_time_in_window` reads
  `current_time.time()` from a UTC instant, so a configured 22:00 fires at
  23:00 in Lagos.

**`home_flex` has no FUP at all.** Zero rules across all five offers; one
orphan policy on Homeflex Basic with nothing under it. Its 63 subscribers are
uncapped on a product defined by its cap.

| Offer | Mbps | Active subs | Policies | Rules |
|---|---|---|---|---|
| Homeflex Starter | 6 | 27 | 0 | 0 |
| Homeflex Basic | 10 | 25 | 1 | **0** |
| Homeflex Elite | 20 | 7 | 0 | 0 |
| Homeflex Elite Plus | 35 | 3 | 0 | 0 |
| Homeflex Premium | 50 | 0 | 0 | 0 |

Still unexpressed:

- `guaranteed_speed` — `none` on every offer, including all 41 dedicated. **No
  dedicated customer currently receives a CIR**, though the emitting code is
  merged and shadow-verified inert.
- `sla_profile_id` — 0 set; `sla_profiles` is empty. No family SLA default is
  recorded, deliberately: the numbers are a commercial decision (§8).
- `policy_set_id` — 0 set, though 2 policy sets exist.
- `pon_ports.downstream_mbps` — 0 of 502 surveyed, so no capacity verdict can
  be reached (§9).
- `aggregation` — dedicated 1:1 (40 of 41, one NULL); unlimited normalised to
  1:5; `home_flex` still split 1/3/5.

An earlier revision of this section claimed `usage_allowance_id` was 0 set and
that FUP did not fire anywhere. That was wrong: the query behind it grouped by
`plan_family` and the six offers carrying allowances were unclassified at the
time, so they fell out of the rollup entirely. Unclassified offers are
invisible to family-scoped analysis — which is the argument for §5's
invariants, not an exception to them.

## 7. Variants are never new offers

A "variant" is any way one commercial product is sold differently. The rule:
**an offer is a product, not a sales situation.** If two rows differ only in who
buys it, where, or how it is taxed, they are one offer with a qualifier.

| Variant | Owner | Mechanism |
|---|---|---|
| Reseller-private plan | catalog availability | `OfferResellerAvailability` |
| Location-restricted | catalog availability | `OfferLocationAvailability` |
| Prepaid vs postpaid | catalog availability | `OfferBillingModeAvailability` |
| VAT-exempt customer | `billing.customer_tax_policy` | `CustomerTaxPolicy.vat_exempt` |
| Withholding tax | `billing.customer_tax_policy` | `CustomerTaxPolicy.withholding_tax_enabled` → `WithholdingTaxRecord` |
| Pro bono / staff | discount on the subscription | `DiscountType.percentage` at 100% |
| Regional price | **gap — see below** | |

`billing_automation.py` already treats the catalog as the service-level tax
authority (a positive `vat_percent` means taxable) and `CustomerTaxPolicy` as
the customer-level authority. A "No VAT" offer would be a **third** authority
over the same question and must not exist.

Pro bono as a discount rather than a ₦0 offer keeps the foregone revenue
visible in reporting; a zero-priced offer hides it.

### The offer-explosion symptom

Ignoring this rule is already visible in production: `STM-1 Fiber
(Norrenberger)`, `200 Mbps Fiber mr richard`, `700 Mbps Dedicated AScomnet` and
`Deen Global Innovation 600Mbps` are customer-named offers. Each is one
negotiated price wearing a whole product row, which is why the catalog carries
duplicate speeds at incompatible prices.

The correct expression of a negotiated price is a subscription-scoped discount
or contract, not a new offer.

### Legacy duplicate cutover

Migration `489_unique_sellable_offer_name` installs the database constraint
that prevents two active, sellable offers from sharing one picker-visible
name. Before installing it, the migration projects two specifically
adjudicated Splynx records to the confirmed production state:

- tariff 71, the zero-price legacy `25 Mbps Fiber`, remains active for
  subscription history but is withdrawn from service and portal selection;
- tariff 79, the superseded 200 Mbps `Unlimited Pro`, is archived, made
  inactive, and withdrawn from service and portal selection.

Both repairs require the stable tariff ID and expected name, and are no-ops
when an environment already matches production. The paid tariff 77 and live
tariff 86 remain untouched. Any other sellable-name collision still fails the
migration closed for explicit operator adjudication.

### Regional pricing — the one real gap

`region_zone_id` sits on `CatalogOffer`, not on `OfferPrice`. So selling the
same product at two prices in two regions currently requires **two offers** —
the very explosion this section forbids. Production confirms the feature is
dormant: one `RegionZone` ("Default Region") and zero offers assigned.

Closing it properly means moving region onto the price: one offer, many
region-scoped `OfferPrice` rows, with a documented fallback to the
region-less price. That keeps one product identity while letting price vary,
and it is the only listed variant that needs a schema change.

## 8. SLA is set per family

`SlaPolicyVersion` carries a `plan_family` scope, resolved by
`customer.service_level`. Precedence, highest first:

1. `subscription_contract` — this customer's negotiated terms
2. `account_contract` — the account's terms
3. `offer_version` — a plan that promises its own SLA
4. **`plan_family`** — the family default
5. `internal_measurement` — what we measure, never what we promised

A family default reaches a subscription through its offer's `plan_family`, so
terms are set once instead of copied onto every offer and left to drift. Terms
are append-only and effective-dated like every other scope: raising a target
opens a new version and closes the old one, so a period already scored keeps
the terms it was measured under.

The family vocabulary is closed in the database as well as the service, so a
direct write cannot introduce a family the resolver has no way to match.

**No default targets are set.** The structure is in place; the numbers are a
commercial decision and must not be invented. Until a family policy is
recorded, resolution falls through to whatever lower-precedence terms exist.

Before any target is committed, see §4 on telemetry — measured availability is
not currently fit to underwrite an SLA.

## 9. Bandwidth is priced from bands, not from rows

`BandwidthPriceBand` + `app/services/bandwidth_pricing.py` own "what does N
Mbps cost". Dedicated circuits sell at arbitrary speeds, so a `CatalogOffer`
row per speed is what produced duplicate speeds at incompatible prices.

Bands are half-open `[speed_from_mbps, speed_to_mbps)` per plan family, top
band left open. **Rates accumulate progressively**, like tax brackets:

    0-10 Mbps @ ₦10,000   ->  10 Mbps = ₦100,000
    10-50 Mbps @ ₦8,000   ->  11 Mbps = ₦100,000 + 1 × ₦8,000 = ₦108,000

The alternative — a band's rate applied to the whole circuit — recreates the
exact defect this replaces: 11 × ₦8,000 = ₦88,000, **cheaper than 10 Mbps**.
Progressive accumulation is monotonic by construction, so no band set can
price more bandwidth cheaper than less. That property is swept across every
boundary in the tests and must not be traded for a simpler sales pitch.

`validate_band_set` refuses a set with an overlap, a gap, a closed top, a
second open top, or mixed currencies. An unquotable speed raises rather than
guessing — inventing a number would put a figure in front of a customer that
no rule produced.

The quote is **advisory and writes nothing**. The contracted figure is captured
on `QuoteLineItem.unit_price` when the quote is raised, so re-rating a band
never rewrites an issued quote. That is why bands need no effective-dating,
unlike SLA terms.

No rates are seeded — they are a commercial decision.

### Not yet an SOT owner

`service_intent.bandwidth_pricing` is deliberately **not** in the registry yet.
`tests/architecture/test_sot_registry_liveness.py` requires a declared owner to
have a real caller, and `test_no_new_uncontracted_manifest_services` requires a
full typed `ServiceContract`. Registering an engine nothing calls would be the
false ownership claim those tests exist to catch. Register it together with its
first consumer — either a read-only quoting endpoint for sales, or a governance
check that flags a dedicated offer priced off-band.

## 10. Transit and layer 2 are handoffs, not catalogs

Transit is a dedicated circuit **delivered over BGP** rather than a static
address. A layer-2 service is dedicated capacity delivered as a **clear
channel with no IP layer**, to a third party who pushes their own addressing
across it. Neither is a different product — only the handoff differs.

So there is no transit catalog and no layer-2 catalog. There is one dedicated
product and a typed delivery specification, `ServiceHandoff`, one row per
subscription:

| Type | Carries | For |
|---|---|---|
| `static_ip` | nothing extra | ordinary internet access |
| `bgp` | customer ASN, announced prefixes, peer IP | transit |
| `layer2_clear_channel` | A-end, B-end, VLAN | carrier clear channel |

A database CHECK binds the fields to the type, so a BGP handoff without an ASN
cannot persist and a clear channel cannot claim an ASN — the failure surfaces
at order time rather than at turn-up. The sales order captures the
requirement; this row is where it lands and what the NOC reads.

Modelling these as plan families would fork the catalog over a delivery detail
— the pattern that already produced customer-named offer rows (§7). Modelling
them as an untyped blob on the sales order would leave provisioning facts with
no schema and no owner.

### IP addresses

Unlimited plans carry no public IP (§1). A customer who needs one buys a
block, which is already modelled: `AddOnType.static_ip` / `extra_ip`, with
`/24, /28, /29, /30, /32` defined in production and `OfferAddOn` carrying
min/max quantity per offer.

**The mechanism works and is entirely unused: all five blocks link to zero
offers.** That is a catalog data gap, not a code gap — no unlimited offer
currently sells a public IP.

The remaining code gap is allocation: an add-on makes a block *quotable* but
nothing records which block went to which customer, so assignment would need a
parallel inventory. Close that before selling one.

## 11. Migration order

1. Create the dedicated SLA profile and a `1:1 Dedicated` policy baseline.
2. Set `guaranteed_speed`/`guaranteed_speed_limit_at` on dedicated offers;
   extend `_rate_limit()` to emit the CIR field. Shadow-diff the generated
   radreply before cutover.
3. Close the three §12 engine gaps — proportional throttle, night release,
   local-time windows — behind tests, before any rule is written that depends
   on them.
4. Give `home_flex` its §1 ladder in `fup_rules`, then convert
   `high_speed_data` from monthly to daily. Verify throttle *and release* on
   one test subscriber per family before fleet rollout.
5. Retire `usage_allowances.throttle_rate_mbps` (§12).
6. Backfill `policy_set_id` across all families.
7. Normalize home_flex `aggregation`; fill the one NULL dedicated offer.
8. Add the section 5 invariants as validation plus architecture tests.
9. Build the PON/BNG capacity reconciler; only then consider publishing any
   contention figure to customers.

Steps 2 and 4 change live subscriber sessions. Each needs a shadow phase, a
named cutover gate, and a verified rollback.

## 12. FUP has one owner — what that costs

§1 names `fup_policies` + `fup_rules` the sole owner of every FUP decision.
Three things currently decide FUP behaviour from outside that owner, and each
must be moved or retired.

### The throttle rate is decided by a global setting

`usage.fup_throttle_radius_profile_id` names one RADIUS profile for every
throttled subscriber on every plan. That is a decision — *how hard do we
throttle* — living outside the owner, and it cannot express §1's proportional
throttle at all.

**Fix:** derive the throttle rate from the offer's own rate and the rule's
`speed_reduction_percent`, making that field load-bearing instead of
decorative. The global profile becomes a fallback for offers with no rate,
not the mechanism. This is the change that turns "throttle to 50%" from
un-expressible into a one-field configuration.

### The night window is decided in two places, and works in neither

`fup_rules.time_start/time_end` gates *triggering*;
`fup_policies.traffic_accounting_start/end` is documented as gating
*accrual* but is never applied to measurement. A free night needs both halves
and one owner.

**Fix:** the rule's window is authoritative for both. Enforcement lifts an
active throttle when no rule triggers — not only at `cap_resets_at` — and
`windowed_used_bytes` subtracts the excluded window from accrual. Both
comparisons happen in the subscriber's local timezone, which
`fup_window_bounds` already resolves and `_time_in_window` currently ignores.

### `usage_allowances.throttle_rate_mbps` is a second answer

It is set on five of six allowances (10, 10, 10, 10 and 1 Mbps) and **read by
nothing** — an admin form field and a CSV column. Production actually throttles
every one of them to 1 Mbps. So the catalogue asserts a post-FUP speed that is
wrong for four of the five, and no code would notice.

**Fix:** drop the column. `included_gb`, `overage_rate`, `overage_cap_gb` and
`rollover_enabled` stay — those are billing, which `usage_allowances`
legitimately owns (§1). Only the enforcement decision leaves.

An unread column holding a wrong answer is worse than a missing one: it is
evidence, and someone will eventually act on it.
