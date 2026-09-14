"""``reconcile_ont`` — the single entry point that composes everything.

Phases (each described in design doc; failure at any phase produces a
``ReconcileResult`` rather than raising):

  1. Acquire row lock — serializes concurrent reconciles, recovers crashed
     prior. Errors: ``OntNotFound`` → ``INVALID_CHANGE``.
  2. Mode-specific guard — ``mode=sync`` refuses against ``out_of_sync`` rows.
     ``mode=sweep`` proceeds (this is how out_of_sync gets cleared).
  3. Materialise current desired state from the ``OntUnit`` row.
  4. Merge proposed_change → target desired state; validate via
     ``validate_desired``. Errors: ``INVALID_CHANGE``.
  5. Build the adapters required by the requested delivery scope. In
     production these are resolved from ``OltDevice`` and ``Tr069AcsServer``;
     explicit WiFi delivery is ACS-only and carries cached OLT evidence.
  6. Read the required live observed state, parallelizing OLT + ACS when both
     surfaces are in scope.
  7. Compute the plan from (target desired, observed, mode).
  8. Precondition: any surface the plan needs that was unreachable at read
     time → fast-fail. Errors: ``OLT_UNREACHABLE`` / ``ACS_UNREACHABLE``.
     Separately, ``compute_plan`` may have refused to build the ACS half at
     all because the device has no ACS document or no unambiguous GenieACS
     ``_id``. The OLT half of such a plan still applies; the pass then reports
     ``ONT_NOT_INFORMING`` / ``ACS_IDENTITY_UNRESOLVED`` instead of claiming
     convergence, and the repeated-fault counter is advanced so a permanently
     undeliverable ONT stops failing silently.
  9. Apply the plan via ``apply_plan``.
 10. Persist: upsert observation; write back desired-state mutations on
     success; set ``sync_status``/``last_error``/``last_reconciled_at``.

The function itself is synchronous. Interactive customer WiFi requests reach
it through the durable configuration worker; operator and sweep callers may
invoke it directly. Long operations take up to ``timeout_sec``.

"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.ppp_delivery_authorization import (
    authorize_ppp_delivery,
    derive_delivery_scope,
    is_ppp_attributable_drift,
)

from .adapters import (
    ACS_DELIVERY_FAULT_REASONS,
    apply_proposed_change,
    clear_acs_delivery_fault,
    desired_from_ont_unit,
    observed_from_ont_observation,
    record_acs_delivery_fault,
    upsert_ont_observation,
)
from .alerts import (
    escalate_acs_delivery_fault,
    resolve_acs_delivery_fault,
    resolve_sweep_unreachable,
)
from .applier import ApplyContext, SecretResolver, apply_plan
from .lifecycle import (
    ReconcileLifecycleBinding,
    bind_reconcile_projection,
    reconcile_binding_matches,
)
from .locking import OntNotFound, acquire_reconcile_lock
from .planner import compute_plan
from .readers import ReadResult, read_acs_state, read_olt_state
from .readers.reachability import PingFunction, is_pingable
from .secrets import default_secret_resolver_from_env
from .state import (
    AcsObservedFields,
    Drift,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
    OntWanProposedChange,
    OntWifiDeliveryScope,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileMode,
    ReconcileResult,
    WriteSurface,
)
from .validator import validate_desired

logger = logging.getLogger(__name__)


_IDLE_TIMEOUT_MARGIN_SEC = 30


def _widen_idle_in_transaction_timeout(db: Session, timeout_sec: int) -> None:
    """Let the lock-held transaction outlive its own device I/O.

    A reconcile pass holds ``SELECT FOR UPDATE`` on the ONT row for the whole
    read/plan/apply cycle — that row lock is what serializes concurrent passes,
    so it cannot be released before the OLT SSH and TR-069 round-trips. Those
    round-trips are slow (each Huawei command costs seconds under slow-send),
    and the connection sits idle-in-transaction while they run.

    Production sets ``idle_in_transaction_session_timeout`` well below a
    realistic reconcile, so Postgres was terminating the connection mid-apply
    and surfacing it as an opaque ``OperationalError``. The transaction must be
    allowed to live at least as long as the reconciler's own deadline.

    ``set_config(..., true)`` scopes this to the current transaction, so it
    reverts on commit or rollback and never leaks to the next user of a pooled
    connection. It is also parameterizable; PostgreSQL does not accept a bound
    parameter in ``SET LOCAL ... = :value``. A setup error must propagate so the
    outer transaction handler can roll back immediately instead of continuing
    with an aborted transaction. Postgres-only; other dialects (SQLite in
    tests) have no such setting.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    budget_ms = (timeout_sec + _IDLE_TIMEOUT_MARGIN_SEC) * 1000
    db.execute(
        text(
            "SELECT set_config("
            "'idle_in_transaction_session_timeout', :timeout_value, true)"
        ),
        {"timeout_value": f"{budget_ms}ms"},
    )


def reconcile_ont(
    db: Session,
    ont_unit_id: str | uuid.UUID,
    *,
    proposed_change: OntWanProposedChange | dict[str, Any] | None = None,
    wifi_delivery_scope: OntWifiDeliveryScope | None = None,
    force_lan_config: bool = False,
    timeout_sec: int = 60,
    mode: ReconcileMode = "sync",
    olt_adapter: Any = None,
    acs_client: Any = None,
    secret_resolver: SecretResolver | None = None,
    ping_function: PingFunction | None = None,
    lifecycle_binding: ReconcileLifecycleBinding | None = None,
    readback_only: bool = False,
) -> ReconcileResult:
    """Reconcile one ONT — bring live state into agreement with desired state.

    Args:
        db: SQLAlchemy session. The caller owns transaction completion. The
            reconciler only flushes status and observation changes inside the
            lock-held transaction.
        ont_unit_id: UUID or string id of the target ONT.
        proposed_change: Optional ``OntDesiredState`` field updates. Legacy
            mappings are persisted after successful reconciliation. A typed
            ``OntWanProposedChange`` represents intent already persisted by
            the admin action and scopes network delivery without writing the
            effective values back as per-ONT overrides. Both forms are
            validated before any network call; rejected changes return
            ``INVALID_CHANGE`` with no network side effects.
        wifi_delivery_scope: Explicit WiFi fields already admitted and stored
            by the configuration lifecycle owner. Values are read from current
            desired state; this scope carries no credential material.
        force_lan_config: Force the admitted LAN/DHCP block to the ACS because
            deployed firmware commonly exposes these fields as write-only.
        timeout_sec: Outer deadline for the whole reconcile (read + plan +
            apply + persist). Default 60s.
        mode: ``sync`` (operator-initiated; refuses ``out_of_sync``),
            ``sweep`` (periodic; proceeds against ``out_of_sync``),
            ``bootstrap`` (BOOTSTRAP event from GenieACS; like sync but
            always force-pushes the WiFi password).
        lifecycle_binding: Exact assignment, configuration head, revision and
            operation allowed to own this pass's current status projection.
        readback_only: Observe and verify without applying a device write.
        olt_adapter: Pre-built OLT SSH adapter. Tests pass a stub; in
            production it's built from the ``OLTDevice`` row.
        acs_client: Pre-built GenieACS NBI client. Same pattern.
        secret_resolver: Maps secret refs to plaintext at apply time.
            Default is selected per-call via
            ``default_secret_resolver_from_env``: OpenBao-backed when
            ``OPENBAO_ADDR`` is set and reachable, otherwise passthrough.
            Tests inject their own resolver for deterministic behaviour.

    Returns:
        A ``ReconcileResult``. The function never raises under normal
        operation; mapping to HTTP responses is the caller's job
        (``ReconcileFailureReason`` constants are the categorization).
    """
    started_monotonic = time.monotonic()
    started_at = datetime.now(UTC)
    deadline = started_at + timedelta(seconds=timeout_sec)
    if secret_resolver is None:
        secret_resolver = default_secret_resolver_from_env()
    proposed_values = (
        proposed_change.as_mapping()
        if isinstance(proposed_change, OntWanProposedChange)
        else proposed_change
    )
    persist_proposed_values = not isinstance(proposed_change, OntWanProposedChange)

    try:
        with acquire_reconcile_lock(db, ont_unit_id) as ont:
            _widen_idle_in_transaction_timeout(db, timeout_sec)
            # ── Mode guard ──────────────────────────────────────────────────
            blocks_current_request = (
                lifecycle_binding is None
                or reconcile_binding_matches(ont, lifecycle_binding)
            )
            if (
                mode == "sync"
                and ont.sync_status == OntSyncStatus.out_of_sync
                # Only HARD out-of-sync blocks. A failed write, a crashed pass
                # or an unreachable surface records `last_error`; PPP work
                # withheld by delivery policy records none. Blocking on the
                # latter would freeze the device permanently, because the
                # refusal recurs every pass by design -- so unrelated
                # management, Wi-Fi and LAN changes could never converge again
                # even though nothing is broken.
                and ont.last_error is not None
                and blocks_current_request
            ):
                # Including just-recovered crashes (lock module sets out_of_sync
                # then yields). Per Hole 7 design: operator must explicitly use
                # sweep/force-reconcile to clear.
                return _failure_result(
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    failure=ReconcileFailure(
                        reason=ReconcileFailureReason.BLOCKED_OUT_OF_SYNC,
                        message=(
                            f"ONT is out_of_sync (last_error: "
                            f"{ont.last_error or 'unknown'}); use sweep/force "
                            "reconcile to clear before retrying."
                        ),
                    ),
                )

            if lifecycle_binding is not None:
                bind_reconcile_projection(ont, lifecycle_binding)

            # Transition to reconciling for the duration of this transaction.
            # If we crash mid-pass, the rollback wipes this and the next
            # reconcile sees the previous status; if we exit normally it gets
            # overwritten with synced/out_of_sync at the end.
            from app.services.network.ont_status import set_sync_status

            set_sync_status(ont, OntSyncStatus.reconciling, reason="reconcile_started")
            ont.last_reconcile_started_at = started_at

            # ── Resolve desired ─────────────────────────────────────────────
            desired_current = desired_from_ont_unit(db, ont)
            proposed_fields: frozenset[str] = (
                frozenset(wifi_delivery_scope.changed_fields)
                if wifi_delivery_scope is not None
                else frozenset()
            )
            if force_lan_config:
                proposed_fields = proposed_fields | frozenset(
                    {
                        "dhcp_enabled",
                        "dhcp_pool_min",
                        "dhcp_pool_max",
                        "dhcp_subnet_mask",
                        "lan_gateway_ip",
                    }
                )
            if proposed_values:
                # Filter the proposed_change to OntDesiredState fields only —
                # callers may have copy-pasted extras.
                allowed = {f for f in vars(desired_current)}
                filtered = {k: v for k, v in proposed_values.items() if k in allowed}
                proposed_fields = frozenset(filtered)
                target = replace(desired_current, **filtered)
                # Validation runs only on actual mutations — the principle is
                # "validate at the write boundary." When proposed_change is
                # absent (sweep / sync no-op), we trust the current DB state.
                validation = validate_desired(target, desired_current)
                if not validation.ok:
                    return _finalise(
                        db,
                        ont,
                        success=False,
                        failure=ReconcileFailure(
                            reason=ReconcileFailureReason.INVALID_CHANGE,
                            message=validation.reason or "invalid proposed change",
                        ),
                        started_monotonic=started_monotonic,
                        observed_after=None,
                        actions_applied=(),
                        drift_before=(),
                        drift_after=(),
                        observed_surfaces=frozenset(),
                        olt_read_status=None,
                    )
            else:
                target = desired_current

            # ── Resolve adapters ────────────────────────────────────────────
            olt_only_profile_change = mode == "sync" and proposed_fields == frozenset(
                {"tr069_profile_id"}
            )
            wifi_only_delivery = wifi_delivery_scope is not None
            if olt_adapter is None and not wifi_only_delivery:
                olt_adapter = _resolve_olt_adapter(db, ont)
            if acs_client is None and not olt_only_profile_change:
                acs_client = _resolve_acs_client(db, ont)
            acs_observed_fallback = (
                _cached_acs_observation(db, ont)
                if olt_only_profile_change and acs_client is None
                else None
            )
            cached_wifi_observation = (
                _cached_observation(db, ont) if wifi_only_delivery else None
            )
            olt_observed_fallback = (
                cached_wifi_observation.olt
                if cached_wifi_observation is not None
                else None
            )
            # ``olt_adapter is None`` at this point means no OLT I/O will
            # happen this pass at all — the WiFi-only delivery path
            # substitutes ``olt_observed_fallback`` (cached prior evidence,
            # or a synthesized absence when there is none) instead of paying
            # for a live SSH read. That substitution is still classified
            # present/absent by ``_read_observed_parallel`` so planning can
            # use it (see ``olt_observed_fallback`` above), but it must not
            # be recorded as this pass having actually observed the OLT
            # surface — ``_surfaces_observed``/``_observed_olt_read_status``
            # key off this flag, not off the substituted status, to keep
            # ``olt_observed_at``/``olt_read_status`` honest. ``olt_adapter``
            # is never reassigned after this point, so the same flag applies
            # to both the pre-apply read below and the post-apply verify read.
            olt_read_attempted = olt_adapter is not None
            # Symmetric case: ``acs_client is None`` at this point means the
            # OLT-only profile-change pass (``olt_only_profile_change``)
            # deliberately skipped ACS I/O and substitutes
            # ``acs_observed_fallback`` (cached prior evidence) instead of
            # paying for a live GenieACS read. Same treatment as
            # ``olt_read_attempted`` above — the substitution is still
            # classified present/absent for planning, but must not be
            # recorded as this pass having actually observed the ACS
            # surface. ``acs_client`` is never reassigned after this point,
            # so the same flag applies to both the pre-apply read below and
            # the post-apply verify read.
            acs_read_attempted = acs_client is not None

            # ── Read observed (parallel OLT + ACS) ──────────────────────────
            olt_result, acs_result = _read_observed_parallel(
                olt_adapter,
                acs_client,
                target,
                deadline=deadline,
                olt_observed_fallback=olt_observed_fallback,
                acs_observed_fallback=acs_observed_fallback,
            )

            # ICMP reachability check on the mgmt IP. Runs from the reconciler
            # host which has the wg0 route to per-OLT mgmt subnets. Doesn't
            # gate the apply pass on its own (the precondition layer uses the
            # OLT/ACS reader unreachable flags) — it's stored on the
            # observation row so the operator UI can flag "ONT mgmt plane
            # was down at last reconcile" without needing to re-ping later.
            if wifi_only_delivery:
                mgmt_ip_pingable = bool(
                    cached_wifi_observation and cached_wifi_observation.mgmt_ip_pingable
                )
            else:
                mgmt_ip_pingable = is_pingable(
                    target.mgmt_ip,
                    ping_function=ping_function,
                )

            observed_before = OntObservedState(
                last_reconciled_at=started_at,
                last_reconcile_duration_ms=int(
                    (time.monotonic() - started_monotonic) * 1000
                ),
                mgmt_ip_pingable=mgmt_ip_pingable,
                consecutive_sweep_unreachable=(ont.consecutive_sweep_unreachable),
                olt=olt_result.observed or _absent_olt(),
                acs=acs_result.observed or _absent_acs(),
            )

            # ── Refuse before planning if the OLT read can't be trusted ─────
            # An unavailable OLT read (transport failure OR a reachable but
            # unparseable/rejected reply) must never be treated as "the
            # device is absent" — that false absence is what previously drove
            # a live OltAuthorize / IPHOST rewrite / WiFi+PSK push against a
            # device we simply failed to observe. Refuse here, before
            # ``compute_plan`` ever sees the substituted placeholder, and
            # preserve whatever OLT evidence is already on file (see
            # ``observed_surfaces`` below).
            if olt_result.status == "unavailable":
                olt_unavailable_reason = _olt_unavailable_reason(olt_result)
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=ReconcileFailure(
                        reason=olt_unavailable_reason,
                        message=(
                            "OLT observation unavailable: "
                            f"{olt_result.error or 'no detail'}"
                        ),
                    ),
                    started_monotonic=started_monotonic,
                    observed_after=observed_before,
                    actions_applied=(),
                    drift_before=(),
                    drift_after=(),
                    observed_surfaces=_surfaces_observed(
                        olt_result,
                        acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        olt_result, read_attempted=olt_read_attempted
                    ),
                )

            # ── Compute plan ────────────────────────────────────────────────
            plan = compute_plan(
                target,
                observed_before,
                mode,
                proposed_fields=proposed_fields,
                force_proposed_writes=not readback_only,
            )

            # ── Precondition: surfaces the plan needs must be reachable ─────
            # OLT-unavailable already returned above, before ``compute_plan``
            # ran at all — this set only ever gains "acs" in practice now,
            # kept as a set for the shared ``blocked`` logic below.
            unreachable: set[WriteSurface] = set()
            if olt_result.unreachable:
                unreachable.add("olt")
            if acs_result.unreachable:
                unreachable.add("acs")
            blocked: set[WriteSurface] = set(plan.required_surfaces & unreachable)
            if not blocked and acs_result.unreachable and plan.waiting_for_acs:
                # The ACS half could not even be planned. When the ACS read
                # itself failed, ``acs_present=False`` means "we could not ask",
                # not "the device has no ACS document" — report the transport
                # failure rather than a wait-for-Inform, and keep the existing
                # fast-fail-before-any-write behaviour.
                blocked.add("acs")
            if blocked:
                # Pick the most-specific failure reason; OLT trumps ACS because
                # OLT writes precede ACS writes in the action order.
                if "olt" in blocked:
                    reason = ReconcileFailureReason.OLT_UNREACHABLE
                    message = (
                        f"OLT {getattr(olt_adapter, 'olt', None)} "
                        f"unreachable: {olt_result.error or 'no detail'}"
                    )
                else:
                    reason = ReconcileFailureReason.ACS_UNREACHABLE
                    message = f"ACS unreachable: {acs_result.error or 'no detail'}"
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=ReconcileFailure(reason=reason, message=message),
                    started_monotonic=started_monotonic,
                    observed_after=observed_before,
                    actions_applied=(),
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
                    observed_surfaces=_surfaces_observed(
                        olt_result,
                        acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        olt_result, read_attempted=olt_read_attempted
                    ),
                )

            # ── Apply ───────────────────────────────────────────────────────
            if readback_only:
                plan_wait = _plan_wait_failure(plan)
                if plan_wait is not None:
                    return _finalise(
                        db,
                        ont,
                        success=False,
                        failure=plan_wait,
                        started_monotonic=started_monotonic,
                        observed_after=observed_before,
                        actions_applied=(),
                        drift_before=plan.drifts,
                        drift_after=plan.drifts,
                        observed_surfaces=_surfaces_observed(
                            olt_result,
                            acs_result,
                            olt_read_attempted=olt_read_attempted,
                            acs_read_attempted=acs_read_attempted,
                        ),
                        olt_read_status=_observed_olt_read_status(
                            olt_result, read_attempted=olt_read_attempted
                        ),
                    )
                if plan.actions or plan.drifts:
                    # ``plan.actions`` empty does NOT mean "no drift" — a
                    # non-repairable drift (e.g. a service port mismatched at
                    # its allocated index) can be recorded with no
                    # corresponding action and no ``olt_wait_reason``. Gating
                    # on drift, not action count, mirrors the write path's own
                    # gate (``verify_debt = verify_plan.drifts + ...`` below)
                    # so a readback pass can never claim convergence, and
                    # therefore can never clear ``out_of_sync``, while
                    # unrelated drift remains on record.
                    return _finalise(
                        db,
                        ont,
                        success=False,
                        failure=ReconcileFailure(
                            reason=ReconcileFailureReason.VERIFICATION_MISMATCH,
                            message="Current revision is still awaiting device readback.",
                            evidence={
                                "readback_pending": True,
                                "drift_fields": [
                                    f"{drift.surface}:{drift.field}"
                                    for drift in plan.drifts
                                ],
                            },
                        ),
                        started_monotonic=started_monotonic,
                        observed_after=observed_before,
                        actions_applied=(),
                        drift_before=plan.drifts,
                        drift_after=plan.drifts,
                        observed_surfaces=_surfaces_observed(
                            olt_result,
                            acs_result,
                            olt_read_attempted=olt_read_attempted,
                            acs_read_attempted=acs_read_attempted,
                        ),
                        olt_read_status=_observed_olt_read_status(
                            olt_result, read_attempted=olt_read_attempted
                        ),
                    )
                return _finalise(
                    db,
                    ont,
                    success=True,
                    failure=None,
                    started_monotonic=started_monotonic,
                    observed_after=observed_before,
                    actions_applied=(),
                    drift_before=plan.drifts,
                    drift_after=(),
                    observed_surfaces=_surfaces_observed(
                        olt_result,
                        acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        olt_result, read_attempted=olt_read_attempted
                    ),
                )

            # Resolve delivery-time PPP authorization from the service-intent
            # model. This is deliberately not derived from the plan: the plan
            # says what desired state wants, and this says whether the service
            # is allowed to terminate PPP on this ONT at all.
            # The ruling is issued against the PPP content of THIS plan, and
            # the scope is derived independently from live state plus the same
            # plan. Comparing them at apply time is a real check; echoing the
            # ruling's own fields back at it -- which an earlier version did --
            # compared the ruling against itself and enforced nothing.
            ppp_ruling = None
            ppp_scope = None
            if not wifi_only_delivery:
                ppp_ruling = authorize_ppp_delivery(db, ont, actions=plan.actions)
                ppp_scope = derive_delivery_scope(db, ont, plan.actions)
                if not ppp_ruling.authorized:
                    logger.info(
                        "reconcile_ppp_delivery_not_authorized",
                        extra=ppp_ruling.as_log_extra(),
                    )
            ctx = ApplyContext(
                olt_adapter=olt_adapter,
                acs_client=acs_client,
                resolve_secret=secret_resolver,
                ppp_authorization=ppp_ruling,
                ppp_scope=ppp_scope,
            )
            apply_outcome = apply_plan(plan, ctx, deadline=deadline)

            if not apply_outcome.success:
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=apply_outcome.halted_by,
                    started_monotonic=started_monotonic,
                    observed_after=observed_before,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
                    observed_surfaces=_surfaces_observed(
                        olt_result,
                        acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        olt_result, read_attempted=olt_read_attempted
                    ),
                )

            # Reset the sweep-unreachable counter on any successful reconcile.
            # Capture the prior value so we can fire a resolution alert when
            # recovering from a previously-alerting unreachable streak.
            prior_unreachable = ont.consecutive_sweep_unreachable or 0
            ont.consecutive_sweep_unreachable = 0
            if prior_unreachable > 0:
                resolve_sweep_unreachable(
                    ont_id=str(ont.id),
                    serial_number=str(ont.serial_number or ""),
                    mgmt_ip=target.mgmt_ip,
                    before=prior_unreachable,
                )

            # PPP work withheld by delivery authorization is residual drift,
            # not convergence. `synced` is defined as "zero residual drift", so
            # reporting it would state that a device matches desired state
            # while its dialer actions were deliberately never sent -- the
            # containment would vanish from every surface that reads
            # sync_status. It is still not a failure: everything unrelated
            # converged and no write was rejected, so no `last_error` is
            # recorded and the sweeper must not retry-storm against it.
            residual_ppp_drift = tuple(
                Drift(
                    field=f"ppp_delivery[{refused.action_name}]",
                    surface="acs" if refused.action_name.startswith("Acs") else "olt",
                    desired=refused.refusal,
                    observed=None,
                    repairable=False,
                )
                for refused in apply_outcome.refused_ppp
            )

            # ── Verification re-read ────────────────────────────────────────
            # No-drift-tolerance: refuse to acknowledge convergence unless we
            # can re-read and confirm the planner produces an empty plan
            # against the post-apply state. If actions_applied is empty
            # (drift was zero from the start), there is nothing to verify.
            plan_wait = _plan_wait_failure(plan)

            # A plan whose every action was refused also applies nothing, and
            # must not take the "nothing to verify" shortcut: that path reports
            # zero drift, which is exactly the false convergence this gate
            # exists to prevent.
            if not apply_outcome.actions_applied and not residual_ppp_drift:
                if plan_wait is not None:
                    return _finalise(
                        db,
                        ont,
                        success=False,
                        failure=plan_wait,
                        started_monotonic=started_monotonic,
                        observed_after=observed_before,
                        actions_applied=(),
                        drift_before=plan.drifts,
                        drift_after=plan.drifts,
                        observed_surfaces=_surfaces_observed(
                            olt_result,
                            acs_result,
                            olt_read_attempted=olt_read_attempted,
                            acs_read_attempted=acs_read_attempted,
                        ),
                        olt_read_status=_observed_olt_read_status(
                            olt_result, read_attempted=olt_read_attempted
                        ),
                    )
                if proposed_values and persist_proposed_values:
                    apply_proposed_change(
                        ont,
                        target,
                        changed_fields=proposed_fields,
                    )
                return _finalise(
                    db,
                    ont,
                    success=True,
                    failure=None,
                    started_monotonic=started_monotonic,
                    observed_after=observed_before,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=residual_ppp_drift,
                    observed_surfaces=_surfaces_observed(
                        olt_result,
                        acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        olt_result, read_attempted=olt_read_attempted
                    ),
                )

            verify_olt_result, verify_acs_result = _read_observed_parallel(
                olt_adapter,
                acs_client,
                target,
                deadline=deadline,
                olt_observed_fallback=olt_observed_fallback,
                acs_observed_fallback=acs_observed_fallback,
            )
            observed_after = OntObservedState(
                last_reconciled_at=started_at,
                last_reconcile_duration_ms=int(
                    (time.monotonic() - started_monotonic) * 1000
                ),
                mgmt_ip_pingable=(
                    mgmt_ip_pingable
                    if wifi_only_delivery
                    else is_pingable(
                        target.mgmt_ip,
                        ping_function=ping_function,
                    )
                ),
                consecutive_sweep_unreachable=0,
                olt=verify_olt_result.observed or _absent_olt(),
                acs=verify_acs_result.observed or _absent_acs(),
            )

            # Verify-read couldn't reach OLT or ACS → can't confirm
            # convergence, refuse to mark synced. An unparseable-but-reachable
            # verify reply is the same hazard as a pre-apply one: substituting
            # absence here would overwrite the just-applied evidence with a
            # fabricated "not there" and could still report false convergence
            # if the plan happened to be a delete-only pass.
            if verify_olt_result.status == "unavailable":
                verify_olt_reason = _olt_unavailable_reason(verify_olt_result)
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=ReconcileFailure(
                        reason=verify_olt_reason,
                        message=(
                            "Post-apply verification could not confirm OLT state: "
                            f"{verify_olt_result.error or 'no detail'}"
                        ),
                    ),
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
                    observed_surfaces=_surfaces_observed(
                        verify_olt_result,
                        verify_acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        verify_olt_result, read_attempted=olt_read_attempted
                    ),
                )
            if verify_acs_result.status == "unavailable":
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=ReconcileFailure(
                        reason=ReconcileFailureReason.ACS_UNREACHABLE,
                        message=(
                            "Post-apply verification could not reach ACS: "
                            f"{verify_acs_result.error or 'no detail'}"
                        ),
                    ),
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
                    observed_surfaces=_surfaces_observed(
                        verify_olt_result,
                        verify_acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        verify_olt_result, read_attempted=olt_read_attempted
                    ),
                )

            verify_plan = compute_plan(
                target,
                observed_after,
                mode,
                proposed_fields=proposed_fields,
                force_proposed_writes=False,
            )
            # Every planned service-port CREATE also recorded an entry in
            # ``verification_debt`` (never observable as ordinary "drift" at
            # plan time — the port doesn't exist yet, so there is no
            # divergent value to diff against). Combined here — rather than
            # trusting ``verify_plan.drifts`` alone, or the much weaker
            # ``verify_plan.actions`` (which bootstrap mode always
            # repopulates with the WiFi/PSK push regardless of whether
            # anything is actually wrong) — so a create the OLT silently
            # no-op'd cannot re-plan as "another driftless create" and slip
            # past this gate reporting false convergence.
            verify_debt = verify_plan.drifts + verify_plan.verification_debt

            # Drift that only exists because delivery authorization withheld
            # the repair is not a verification failure -- the reconciler did
            # not fail to converge it, it was forbidden from trying. Left
            # unfiltered this would hard-fail every pass on a contained ONT
            # forever, which is indistinguishable from a broken device.
            # Attribution is per DRIFT, not per action: a non-repairable
            # drift carries no action at all, so an "every action is PPP"
            # verdict would exempt unrelated residual drift that happens to
            # have no repair. Every drift not attributable to the withheld PPP
            # work is preserved and still fails verification.
            unattributed = tuple(
                drift for drift in verify_debt if not is_ppp_attributable_drift(drift)
            )
            if verify_debt and residual_ppp_drift and not unattributed:
                return _finalise(
                    db,
                    ont,
                    success=True,
                    failure=None,
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=residual_ppp_drift,
                    observed_surfaces=_surfaces_observed(
                        verify_olt_result,
                        verify_acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        verify_olt_result, read_attempted=olt_read_attempted
                    ),
                )

            if verify_debt:
                # Classify residual drift as ACS inform-lag or genuine. This
                # reconcile pass still returns VERIFICATION_MISMATCH; the
                # durable configuration lifecycle uses readback_pending
                # evidence to schedule bounded delayed verification.
                _cache_lag, _genuine = _classify_verify_drifts(
                    verify_debt, apply_outcome.actions_applied
                )
                logger.warning(
                    "acs_verify_mismatch",
                    extra={
                        "event": "acs_verify_mismatch",
                        "ont_id": str(ont.id),
                        "mode": mode,
                        "total_drifts": len(verify_debt),
                        "acs_cache_lag_candidates": len(_cache_lag),
                        "genuine_drifts": len(_genuine),
                        # If true, a lifecycle owner may wait for a fresh Inform
                        # rather than treating stale cache as a terminal fault.
                        "would_be_graced": not _genuine,
                        "drift_fields": [f"{d.surface}:{d.field}" for d in verify_debt],
                    },
                )
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=ReconcileFailure(
                        reason=ReconcileFailureReason.VERIFICATION_MISMATCH,
                        message=(
                            "Post-apply state still diverges from desired: "
                            f"{_summarise_drifts(verify_debt)}"
                        ),
                        evidence={
                            "readback_pending": not _genuine,
                            "drift_fields": [
                                f"{drift.surface}:{drift.field}"
                                for drift in verify_debt
                            ],
                        },
                    ),
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=verify_debt,
                    observed_surfaces=_surfaces_observed(
                        verify_olt_result,
                        verify_acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        verify_olt_result, read_attempted=olt_read_attempted
                    ),
                )

            if plan_wait is not None:
                # Either the ACS half was never planned (no ACS document, or
                # no unambiguous GenieACS ``_id``) or the OLT half withheld
                # its service-port deletes (both indices unallocated).
                # Reporting success here would tell an operator the device
                # was fully configured when part of the plan was withheld.
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=plan_wait,
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
                    observed_surfaces=_surfaces_observed(
                        verify_olt_result,
                        verify_acs_result,
                        olt_read_attempted=olt_read_attempted,
                        acs_read_attempted=acs_read_attempted,
                    ),
                    olt_read_status=_observed_olt_read_status(
                        verify_olt_result, read_attempted=olt_read_attempted
                    ),
                )

            if proposed_values and persist_proposed_values:
                apply_proposed_change(
                    ont,
                    target,
                    changed_fields=proposed_fields,
                )
            return _finalise(
                db,
                ont,
                success=True,
                failure=None,
                started_monotonic=started_monotonic,
                observed_after=observed_after,
                actions_applied=apply_outcome.actions_applied,
                drift_before=plan.drifts,
                drift_after=residual_ppp_drift,
                observed_surfaces=_surfaces_observed(
                    verify_olt_result,
                    verify_acs_result,
                    olt_read_attempted=olt_read_attempted,
                    acs_read_attempted=acs_read_attempted,
                ),
                olt_read_status=_observed_olt_read_status(
                    verify_olt_result, read_attempted=olt_read_attempted
                ),
            )

    except OntNotFound as exc:
        # Lock module raised before we entered the work — no row to mutate.
        return _failure_result(
            started_at=started_at,
            started_monotonic=started_monotonic,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.INVALID_CHANGE,
                message=str(exc),
            ),
        )

    except Exception as exc:
        # Unexpected internal error. Transaction completion remains with the
        # public owner; a nested rollback would break atomic coordination.
        logger.exception(
            "reconcile_ont_internal_error",
            extra={"ont_unit_id": str(ont_unit_id), "error": str(exc)},
        )
        return _failure_result(
            started_at=started_at,
            started_monotonic=started_monotonic,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.OLT_WRITE_REJECTED,
                message=f"internal error: {exc}",
            ),
        )


# ── Internal helpers ────────────────────────────────────────────────────────


def _acs_wait_failure(plan) -> ReconcileFailure | None:
    """Translate a plan's ACS identity/presence gate into a failure record.

    ``Plan.acs_wait_reason`` is set when ``compute_plan`` refused to emit ACS
    actions because the device has no ACS document, more than one matched, or
    its recorded and reported GenieACS ids disagree. That is not convergence:
    the OLT half may have been applied, but nothing reached the CPE.
    """
    if plan.acs_wait_reason is None:
        return None
    return ReconcileFailure(
        reason=plan.acs_wait_reason,
        message=plan.acs_wait_detail or "ACS configuration could not be delivered.",
        evidence={"acs_wait_reason": plan.acs_wait_reason},
    )


def _olt_wait_failure(plan) -> ReconcileFailure | None:
    """Translate a plan's OLT service-port-index gate into a failure record.

    ``Plan.olt_wait_reason`` is set when ``compute_plan`` withheld every
    ``OltDeleteServicePort`` action for this ONT because neither the mgmt nor
    the WAN service-port index has been allocated. Deleting every observed
    service port in that state would be unconditional and unrecoverable (the
    create branch requires a real index, so nothing would be recreated), so
    the planner refuses instead of claiming convergence.
    """
    if plan.olt_wait_reason is None:
        return None
    return ReconcileFailure(
        reason=plan.olt_wait_reason,
        message=(
            plan.olt_wait_detail
            or "OLT service-port index is unallocated; refusing to delete "
            "observed ports."
        ),
        evidence={"olt_wait_reason": plan.olt_wait_reason},
    )


def _plan_wait_failure(plan) -> ReconcileFailure | None:
    """Combine the OLT and ACS wait gates into a single failure, if either fired.

    OLT is checked first: its actions precede ACS in write order, and an
    unallocated service-port index is a more fundamental refusal (nothing
    named it, versus ACS merely being unreachable/ambiguous).
    """
    return _olt_wait_failure(plan) or _acs_wait_failure(plan)


def _resolve_olt_adapter(db: Session, ont: OntUnit) -> Any:
    """Production path: build an OLT adapter from the ONT's OLT binding.

    The lazy import keeps the reconcile package importable in test contexts
    where the heavier protocol-adapter module would pull in SSH dependencies.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    if ont.olt_device_id is None:
        raise RuntimeError(
            f"ONT {ont.id} has no olt_device_id — cannot build OLT adapter"
        )
    olt = db.execute(
        select(OLTDevice).where(OLTDevice.id == ont.olt_device_id)
    ).scalar_one()
    return get_protocol_adapter(olt)


def _resolve_acs_client(db: Session, ont: OntUnit) -> Any:
    """Build the client from observed linkage, then desired ACS policy.

    During an ACS migration the ONT still informs to the previously linked
    server. That server must deliver the ManagementServer URL change; the link
    is moved to desired authority only after successful readback.
    """
    from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
    from app.services.genieacs_client import create_genieacs_client
    from app.services.network.acs_resolution import resolve_acs_for_ont

    linked = db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    ).first()
    if linked is not None:
        observed_server = db.get(Tr069AcsServer, linked.acs_server_id)
        if observed_server is not None and observed_server.base_url:
            return create_genieacs_client(observed_server.base_url)

    resolution = resolve_acs_for_ont(db, ont)
    if resolution.server is None:
        raise RuntimeError(f"ONT {ont.id} has no active ACS assignment")
    return create_genieacs_client(resolution.server.base_url)


def _read_observed_parallel(
    olt_adapter: Any,
    acs_client: Any,
    desired: OntDesiredState,
    *,
    deadline: datetime,
    olt_observed_fallback: OltObservedFields | None = None,
    acs_observed_fallback: AcsObservedFields | None = None,
):
    """Run OLT + ACS reads in parallel.

    Two I/O-bound calls, one each — a small thread pool is the right fit.
    The readers themselves don't share state, so no synchronisation needed.
    """
    if olt_adapter is None:
        # No adapter means the caller deliberately skipped OLT I/O (the
        # WiFi-only delivery path substitutes the last cached observation
        # instead of paying for a live SSH read) — never that a real read
        # was attempted and came back empty. The caller (``reconcile_ont``)
        # is responsible for treating this ``ReadResult`` as a cached
        # substitution rather than a fresh observation — see
        # ``_surfaces_observed``'s ``olt_read_attempted`` parameter.
        olt_observed = olt_observed_fallback or _absent_olt()
        olt_result = ReadResult(
            status="present" if olt_observed.olt_present else "absent",
            observed=olt_observed,
            error=None,
        )
        if acs_client is None:
            acs_observed = acs_observed_fallback or _absent_acs()
            return (
                olt_result,
                ReadResult(
                    status="present" if acs_observed.acs_present else "absent",
                    observed=acs_observed,
                    error=None,
                ),
            )
        return olt_result, read_acs_state(acs_client, desired, deadline=deadline)
    if acs_client is None:
        acs_observed = acs_observed_fallback or _absent_acs()
        return (
            read_olt_state(olt_adapter, desired, deadline=deadline),
            ReadResult(
                status="present" if acs_observed.acs_present else "absent",
                observed=acs_observed,
                error=None,
            ),
        )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="reconcile-read") as pool:
        olt_future = pool.submit(
            read_olt_state, olt_adapter, desired, deadline=deadline
        )
        acs_future = pool.submit(read_acs_state, acs_client, desired, deadline=deadline)
        olt_result = olt_future.result()
        acs_result = acs_future.result()
    return olt_result, acs_result


def _cached_acs_observation(db: Session, ont: OntUnit) -> AcsObservedFields:
    """Carry forward ACS reality when an OLT-only reconcile skips ACS I/O."""
    from app.models.ont_observation import OntObservation

    row = db.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
    ).first()
    observed = observed_from_ont_observation(row)
    return observed.acs if observed is not None else _absent_acs()


def _cached_observation(db: Session, ont: OntUnit) -> OntObservedState | None:
    """Load cached non-ACS evidence for an explicitly ACS-only delivery."""

    from app.models.ont_observation import OntObservation

    row = db.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
    ).first()
    return observed_from_ont_observation(row)


def _absent_olt() -> OltObservedFields:
    return OltObservedFields(
        olt_present=False,
        olt_match_state=None,
        olt_run_state=None,
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_tr069_profile_id=None,
        olt_service_ports=(),
    )


def _absent_acs() -> AcsObservedFields:
    return AcsObservedFields(
        acs_present=False,
        acs_last_inform_at=None,
        acs_last_boot_at=None,
        acs_last_bootstrap_at=None,
        acs_observed_software_version=None,
        acs_observed_pppoe_username=None,
        acs_observed_pppoe_enable=None,
        acs_observed_wan_vlan=None,
        acs_observed_wan_external_ip=None,
        acs_observed_wan_connection_status=None,
        acs_observed_nat_enabled=None,
        acs_observed_dhcp_enabled=None,
        acs_observed_ssid=None,
        acs_observed_periodic_inform_interval_sec=None,
        acs_observed_cr_username=None,
        acs_observed_cr_username_set=None,
        acs_observed_cr_password_set=None,
        acs_observed_wan_wcd_index=None,
        acs_observed_wan_instance_index=None,
        acs_observed_wan_ppp_locations=(),
    )


def _olt_unavailable_reason(result: ReadResult) -> str:
    """Map an ``"unavailable"`` OLT ``ReadResult`` to its specific failure reason.

    Three distinct causes share the tri-state's ``"unavailable"`` bucket —
    couldn't contact the device at all, a reachable-but-untrustworthy reply,
    and a physical-identity problem (Astra Bug 1: a registration found at a
    different fsp/onu_id than the desired target, or with no target to
    compare against at all) — and each gets its own operator-facing reason
    rather than collapsing into the generic ``OLT_OBSERVATION_UNAVAILABLE``.
    """
    if result.unreachable:
        return ReconcileFailureReason.OLT_UNREACHABLE
    if result.identity_status == "mismatch":
        return ReconcileFailureReason.OLT_IDENTITY_MISMATCH
    if result.identity_status == "unresolved":
        return ReconcileFailureReason.OLT_IDENTITY_UNRESOLVED
    return ReconcileFailureReason.OLT_OBSERVATION_UNAVAILABLE


def _surfaces_observed(
    olt_result: ReadResult,
    acs_result: ReadResult,
    *,
    olt_read_attempted: bool = True,
    acs_read_attempted: bool = True,
) -> frozenset[WriteSurface]:
    """Which surfaces this pass actually produced a trustworthy read for.

    ``"unavailable"`` on either surface means that surface's half of
    ``observed_before``/``observed_after`` is a synthesized placeholder, not
    real evidence — ``upsert_ont_observation`` must not let it overwrite the
    last genuine observation for that surface.

    ``olt_read_attempted``/``acs_read_attempted`` are each a separate axis
    from their respective result's ``.status``: a WiFi-only delivery pass
    never builds an OLT adapter at all, and an OLT-only profile-change pass
    never builds an ACS client at all — both substitute the last cached
    observation instead (see ``reconcile_ont`` and
    ``_read_observed_parallel``). That substitution is still classified
    ``"present"``/``"absent"`` — the cached data is real and usable for
    planning — but no I/O happened this pass for that surface, so it must
    not count as this pass having observed it. Callers that never skip a
    given surface's I/O pass no override for that flag and get the original
    behaviour.
    """
    surfaces: set[WriteSurface] = set()
    if olt_result.status != "unavailable" and olt_read_attempted:
        surfaces.add("olt")
    if acs_result.status != "unavailable" and acs_read_attempted:
        surfaces.add("acs")
    return frozenset(surfaces)


def _observed_olt_read_status(
    result: ReadResult, *, read_attempted: bool = True
) -> str | None:
    """The freshness signal to stamp on the observation row's ``olt_read_status``.

    ``None`` when this pass performed no real OLT I/O (``read_attempted ==
    False`` — a WiFi-only delivery pass substituting cached OLT data).
    Stamping ``result.status`` in that case would record a live
    present/absent verdict for a read that never happened.
    ``upsert_ont_observation`` treats ``None`` as "leave the column exactly
    as it was," not as "clear it."
    """
    return result.status if read_attempted else None


def _finalise(
    db: Session,
    ont: OntUnit,
    *,
    success: bool,
    failure: ReconcileFailure | None,
    started_monotonic: float,
    observed_after: OntObservedState | None,
    actions_applied,
    drift_before,
    drift_after,
    observed_surfaces: frozenset[WriteSurface],
    olt_read_status: str | None,
) -> ReconcileResult:
    """Persist state on the OntUnit row + observation table, build the result.

    Called inside the lock context — the caller's transaction commits when
    the context exits normally.

    ``observed_surfaces`` names which of ``observed_after.olt``/``.acs`` are
    real reads this pass — the other surface's columns on the observation row
    are left untouched rather than overwritten with a synthesized "absent".
    ``olt_read_status`` is stamped on the row whenever this pass genuinely
    attempted an OLT read (even one that came back ``"unavailable"``), so the
    row carries an honest freshness signal: "the last successful observation
    is these values, but the last attempt was unavailable at this time." When
    no OLT read was attempted at all — a WiFi-only delivery pass substituting
    cached data, via ``_observed_olt_read_status`` returning ``None`` — the
    column is left exactly as it was; there is no "attempt" to record.
    """
    from app.services.network.ont_status import set_sync_status

    now = datetime.now(UTC)
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)

    if success and drift_after:
        # Converged everything it was allowed to, but something was withheld.
        # Not `synced` (which asserts zero residual drift) and not a failure
        # either -- no write was rejected. The drift stays visible so the
        # withheld PPP work is discoverable rather than silently absent.
        set_sync_status(ont, OntSyncStatus.out_of_sync, reason="drift_withheld")
        ont.last_error = None
    elif success:
        set_sync_status(ont, OntSyncStatus.synced, reason="reconcile_succeeded")
        ont.last_error = None
        cleared = clear_acs_delivery_fault(ont)
        if cleared:
            resolve_acs_delivery_fault(
                ont_id=str(ont.id),
                serial_number=str(ont.serial_number or ""),
                before=cleared,
            )
    else:
        set_sync_status(ont, OntSyncStatus.out_of_sync, reason="reconcile_failed")
        ont.last_error = failure.message if failure else "unknown failure"
        _record_acs_delivery_fault(ont, failure)
    ont.last_reconciled_at = now

    if observed_after is not None:
        upsert_ont_observation(
            db,
            ont.id,
            observed_after,
            observed_surfaces=observed_surfaces,
            olt_read_status=olt_read_status,
        )

    return ReconcileResult(
        success=success,
        # The status actually persisted above, not a re-derivation of it. A
        # pass that withheld PPP work persists `out_of_sync` while still
        # succeeding, so re-deriving from `success` alone made the public
        # result contradict the database row it had just written.
        sync_status=ont.sync_status.value,
        actions_applied=tuple(actions_applied),
        drift_before=tuple(drift_before),
        drift_after=tuple(drift_after),
        observed_after=observed_after,
        failure=failure,
        duration_ms=duration_ms,
        reconciled_at=now,
    )


def _record_acs_delivery_fault(ont: OntUnit, failure: ReconcileFailure | None) -> None:
    """Persist and escalate a repeated ACS-delivery fault on this ONT.

    Without this, an ONT whose ACS half can never be written (unknown device
    identity, a CPE that never informs, a permanently faulting NBI write) is
    re-selected by every sweep — the sweep orders by least-recently-reconciled
    — and fails silently forever. Nothing marked the ONT, so nothing surfaced
    it to an operator.
    """
    reason = getattr(failure, "reason", None)
    if reason not in ACS_DELIVERY_FAULT_REASONS:
        return
    streak = record_acs_delivery_fault(
        ont,
        reason,
        getattr(failure, "message", "") or "",
    )
    escalate_acs_delivery_fault(
        ont_id=str(ont.id),
        serial_number=str(ont.serial_number or ""),
        reason=reason,
        message=getattr(failure, "message", "") or "",
        streak=streak,
    )


def _failure_result(
    *,
    started_at: datetime,
    started_monotonic: float,
    failure: ReconcileFailure,
) -> ReconcileResult:
    """Build a failure result for cases that never enter the lock context
    (ONT not found, internal error before lock acquisition)."""
    now = datetime.now(UTC)
    return ReconcileResult(
        success=False,
        sync_status="out_of_sync",
        actions_applied=(),
        drift_before=(),
        drift_after=(),
        observed_after=None,
        failure=failure,
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        reconciled_at=now,
    )


def _classify_verify_drifts(drifts, actions_applied):
    """Split post-apply verify drift into ACS-cache-lag candidates vs genuine.

    The post-apply re-read pulls ACS fields from the GenieACS CWMP cache, which
    only refreshes on the device's next periodic Inform — so a field we just
    wrote AND that the ACS accepted can still read back as its old value for one
    Inform interval, producing a spurious VERIFICATION_MISMATCH that marks the
    ONT ``out_of_sync`` and (in sync mode) locks it out, so the sweeper re-plans
    / re-applies / re-verifies-stale: an oscillation.

    A drift is a **cache-lag candidate** when it is on the ``acs`` surface, is
    repairable (read-verifiable), and names a field we just wrote this pass
    (present in ``actions_applied`` on the ``acs`` surface). Anything else —
    OLT-surface drift, a field we didn't touch, an unrepairable field — is
    **genuine** divergence that must still fail.

    The current reconcile pass still fails on a mismatch. Lifecycle owners may
    use the classification as evidence for bounded delayed readback instead of
    treating an accepted ACS write with stale cache as a terminal failure.
    """
    applied_acs_fields: set[str] = set()
    for action in actions_applied:
        if getattr(action, "surface", None) != "acs":
            continue
        applied_acs_fields.add(str(action.field))
        evidence = getattr(action, "evidence", None)
        if isinstance(evidence, dict):
            changed_fields = evidence.get("changed_fields")
            if isinstance(changed_fields, (list, tuple)):
                applied_acs_fields.update(str(field) for field in changed_fields)
    cache_lag = [
        d
        for d in drifts
        if d.surface == "acs" and d.repairable and d.field in applied_acs_fields
    ]
    genuine = [d for d in drifts if d not in cache_lag]
    return cache_lag, genuine


def _summarise_drifts(drifts) -> str:
    """One-line summary of residual drift after the verification re-read.

    Surfaces the first three field names; longer lists collapse to
    ``"<a>, <b>, <c>, +N more"`` so the operator UI message stays short.
    """
    fields = [str(getattr(d, "field", "?")) for d in drifts]
    if not fields:
        return "no drift"
    if len(fields) <= 3:
        return ", ".join(fields)
    return f"{', '.join(fields[:3])}, +{len(fields) - 3} more"


__all__ = ("reconcile_ont",)
