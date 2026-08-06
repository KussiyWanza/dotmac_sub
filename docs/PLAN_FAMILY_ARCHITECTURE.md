# Plan family architecture

Normative design for the three commercial plan families and how each is
expressed, enforced and verified. Owner: catalog + RADIUS provisioning.

Companion to `SOT_RELATIONSHIP_MAP.md`. Where this document and the executable
registry in `app/services/sot_relationships.py` disagree, report the conflict —
do not guess.

## 1. The three families

Every family sells the same thing — a line rate — and differs only in what
happens when the network is busy and when the customer uses a lot.

| | `home_flex` | `unlimited` | `dedicated` |
|---|---|---|---|
| Data volume | Metered allowance | Unmetered | Unmetered |
| On exhausting allowance | **Throttled** to `throttle_rate_mbps` | n/a — no allowance | n/a |
| Rate under congestion | Best effort | **Best effort** | **Guaranteed (CIR = MIR)** |
| Contention | Shared | Shared | **1:1** |
| Public IP | No | No | **Yes** |
| SLA credits | No | No | Yes |

The defining sentence for each:

- **home_flex** — cheap entry. You get a volume allowance; past it you keep
  working at a reduced speed rather than losing service.
- **unlimited** — no volume limit and no throttle, ever. Speed is best effort:
  the tier rate is a ceiling, not a floor.
- **dedicated** — the tier rate is a floor as well as a ceiling, reserved 1:1,
  with a public IP and SLA credits.

`unlimited` therefore makes **no contention commitment**. It is explicitly best
effort. `CatalogOffer.aggregation` on an unlimited offer is an internal
capacity-planning target, not a customer promise, and must not be published as
one.

## 2. Field mapping

The schema already models all of this. Nothing new is required.

| Concept | Field | home_flex | unlimited | dedicated |
|---|---|---|---|---|
| Volume allowance | `usage_allowance_id` | **set** | NULL | NULL |
| Post-FUP speed | `UsageAllowance.throttle_rate_mbps` | **set** | — | — |
| Rate floor | `guaranteed_speed` | `none` | `none` | **`fixed`** |
| Floor value | `guaranteed_speed_limit_at` | NULL | NULL | **= line rate** |
| Contention target | `aggregation` | shared | shared | **1** |
| Public IP | bundled `AddOn` (`static_ip`) | — | — | **bundled** |
| Service credits | `sla_profile_id` | NULL | NULL | **set** |
| Billing behaviour | `policy_set_id` | set | set | set |

`sla_profiles` is currently empty; a dedicated SLA profile must be created
before dedicated offers can reference one.

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

As at 2026-08-05, across 67 active offers:

- `usage_allowance_id` — **0 set**. home_flex FUP does not fire.
- `sla_profile_id` — **0 set**; `sla_profiles` table is empty.
- `policy_set_id` — **0 set**, though 2 policy sets exist.
- `guaranteed_speed` — `none` on every offer, including all 41 dedicated. **No
  dedicated customer currently receives a CIR.**
- `aggregation` — dedicated 1:1 (40 of 41, one NULL); unlimited normalized to 5;
  home_flex still split 1/3/5.

The families are today distinguished only by naming convention. Every guarantee
described in this document is currently unexpressed in the data.

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
3. Wire `usage_allowance_id` on home_flex offers; verify the throttle fires and
   clears end-to-end on one test subscriber before fleet rollout.
4. Backfill `policy_set_id` across all families.
5. Normalize home_flex `aggregation`; fill the one NULL dedicated offer.
6. Add the section 5 invariants as validation plus architecture tests.
7. Build the PON/BNG capacity reconciler; only then consider publishing any
   contention figure to customers.

Steps 2 and 3 change live subscriber sessions. Each needs a shadow phase, a
named cutover gate, and a verified rollback.
