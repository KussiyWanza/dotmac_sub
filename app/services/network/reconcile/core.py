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
  5. Build target adapters (OLT SSH adapter + GenieACS client). In production
     these are resolved from the ``OltDevice`` and ``Tr069AcsServer`` rows;
     tests inject stubs.
  6. Read OLT + ACS observed state in parallel (two threads, I/O-bound).
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

The function is synchronous (request-scoped) per the "no queue, no silent
failure" design. Long operations (e.g. a full bootstrap) take up to
``timeout_sec`` — operators expect this to be measured in tens of seconds.

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
    timeout_sec: int = 60,
    mode: ReconcileMode = "sync",
    olt_adapter: Any = None,
    acs_client: Any = None,
    secret_resolver: SecretResolver | None = None,
    ping_function: PingFunction | None = None,
) -> ReconcileResult:
    """Reconcile one ONT — bring live state into agreement with desired state.

    Args:
        db: SQLAlchemy session. The reconciler commits at the end on success;
            on failure the caller's transaction lifecycle determines whether
            partial DB writes (status/last_error) persist — the reconciler
            does its writes inside the lock-held transaction.
        ont_unit_id: UUID or string id of the target ONT.
        proposed_change: Optional ``OntDesiredState`` field updates. Legacy
            mappings are persisted after successful reconciliation. A typed
            ``OntWanProposedChange`` represents intent already persisted by
            the admin action and scopes network delivery without writing the
            effective values back as per-ONT overrides. Both forms are
            validated before any network call; rejected changes return
            ``INVALID_CHANGE`` with no network side effects.
        timeout_sec: Outer deadline for the whole reconcile (read + plan +
            apply + persist). Default 60s.
        mode: ``sync`` (operator-initiated; refuses ``out_of_sync``),
            ``sweep`` (periodic; proceeds against ``out_of_sync``),
            ``bootstrap`` (BOOTSTRAP event from GenieACS; like sync but
            always force-pushes the WiFi password).
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

            # Transition to reconciling for the duration of this transaction.
            # If we crash mid-pass, the rollback wipes this and the next
            # reconcile sees the previous status; if we exit normally it gets
            # overwritten with synced/out_of_sync at the end.
            ont.sync_status = OntSyncStatus.reconciling
            ont.last_reconcile_started_at = started_at

            # ── Resolve desired ─────────────────────────────────────────────
            desired_current = desired_from_ont_unit(db, ont)
            proposed_fields: frozenset[str] = frozenset()
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
                    )
            else:
                target = desired_current

            # ── Resolve adapters ────────────────────────────────────────────
            olt_only_profile_change = mode == "sync" and proposed_fields == frozenset(
                {"tr069_profile_id"}
            )
            if olt_adapter is None:
                olt_adapter = _resolve_olt_adapter(db, ont)
            if acs_client is None and not olt_only_profile_change:
                acs_client = _resolve_acs_client(db, ont)
            acs_observed_fallback = (
                _cached_acs_observation(db, ont)
                if olt_only_profile_change and acs_client is None
                else None
            )

            # ── Read observed (parallel OLT + ACS) ──────────────────────────
            olt_result, acs_result = _read_observed_parallel(
                olt_adapter,
                acs_client,
                target,
                deadline=deadline,
                acs_observed_fallback=acs_observed_fallback,
            )

            # ICMP reachability check on the mgmt IP. Runs from the reconciler
            # host which has the wg0 route to per-OLT mgmt subnets. Doesn't
            # gate the apply pass on its own (the precondition layer uses the
            # OLT/ACS reader unreachable flags) — it's stored on the
            # observation row so the operator UI can flag "ONT mgmt plane
            # was down at last reconcile" without needing to re-ping later.
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

            # ── Compute plan ────────────────────────────────────────────────
            plan = compute_plan(
                target,
                observed_before,
                mode,
                proposed_fields=proposed_fields,
            )

            # ── Precondition: surfaces the plan needs must be reachable ─────
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
                )

            # ── Apply ───────────────────────────────────────────────────────
            # Resolve delivery-time PPP authorization from the service-intent
            # model. This is deliberately not derived from the plan: the plan
            # says what desired state wants, and this says whether the service
            # is allowed to terminate PPP on this ONT at all.
            # The ruling is issued against the PPP content of THIS plan, and
            # the scope is derived independently from live state plus the same
            # plan. Comparing them at apply time is a real check; echoing the
            # ruling's own fields back at it -- which an earlier version did --
            # compared the ruling against itself and enforced nothing.
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
            acs_wait = _acs_wait_failure(plan)

            # A plan whose every action was refused also applies nothing, and
            # must not take the "nothing to verify" shortcut: that path reports
            # zero drift, which is exactly the false convergence this gate
            # exists to prevent.
            if not apply_outcome.actions_applied and not residual_ppp_drift:
                if acs_wait is not None:
                    return _finalise(
                        db,
                        ont,
                        success=False,
                        failure=acs_wait,
                        started_monotonic=started_monotonic,
                        observed_after=observed_before,
                        actions_applied=(),
                        drift_before=plan.drifts,
                        drift_after=plan.drifts,
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
                )

            verify_olt_result, verify_acs_result = _read_observed_parallel(
                olt_adapter,
                acs_client,
                target,
                deadline=deadline,
                acs_observed_fallback=acs_observed_fallback,
            )
            observed_after = OntObservedState(
                last_reconciled_at=started_at,
                last_reconcile_duration_ms=int(
                    (time.monotonic() - started_monotonic) * 1000
                ),
                mgmt_ip_pingable=is_pingable(
                    target.mgmt_ip,
                    ping_function=ping_function,
                ),
                consecutive_sweep_unreachable=0,
                olt=verify_olt_result.observed or _absent_olt(),
                acs=verify_acs_result.observed or _absent_acs(),
            )

            # Verify-read couldn't reach OLT or ACS → can't confirm
            # convergence, refuse to mark synced.
            if verify_olt_result.unreachable:
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=ReconcileFailure(
                        reason=ReconcileFailureReason.OLT_UNREACHABLE,
                        message=(
                            "Post-apply verification could not reach OLT: "
                            f"{verify_olt_result.error or 'no detail'}"
                        ),
                    ),
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
                )
            if verify_acs_result.unreachable:
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
                )

            verify_plan = compute_plan(
                target,
                observed_after,
                mode,
                proposed_fields=proposed_fields,
                force_proposed_writes=False,
            )
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
                drift
                for drift in verify_plan.drifts
                if not is_ppp_attributable_drift(drift)
            )
            if verify_plan.drifts and residual_ppp_drift and not unattributed:
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
                )

            if verify_plan.drifts:
                # AUDIT-ONLY (dry-run for the future ACS verify-read grace):
                # classify the residual drift and record whether this mismatch
                # is purely ACS inform-lag (would_be_graced) vs genuine. Behaviour
                # is unchanged — we still fail with VERIFICATION_MISMATCH — so we
                # can quantify the oscillation cause before enabling any grace.
                _cache_lag, _genuine = _classify_verify_drifts(
                    verify_plan.drifts, apply_outcome.actions_applied
                )
                logger.warning(
                    "acs_verify_mismatch",
                    extra={
                        "event": "acs_verify_mismatch",
                        "ont_id": str(ont.id),
                        "mode": mode,
                        "total_drifts": len(verify_plan.drifts),
                        "acs_cache_lag_candidates": len(_cache_lag),
                        "genuine_drifts": len(_genuine),
                        # If True, the future grace would treat this as
                        # converged-pending-reinform instead of out_of_sync.
                        "would_be_graced": not _genuine,
                        "drift_fields": [
                            f"{d.surface}:{d.field}" for d in verify_plan.drifts
                        ],
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
                            f"{_summarise_drifts(verify_plan.drifts)}"
                        ),
                    ),
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=verify_plan.drifts,
                )

            if acs_wait is not None:
                # The OLT half converged, but the ACS half was never planned:
                # this device has no ACS document, or no unambiguous GenieACS
                # ``_id``. Reporting success here would tell an operator the
                # CPE was configured when nothing was delivered to it.
                return _finalise(
                    db,
                    ont,
                    success=False,
                    failure=acs_wait,
                    started_monotonic=started_monotonic,
                    observed_after=observed_after,
                    actions_applied=apply_outcome.actions_applied,
                    drift_before=plan.drifts,
                    drift_after=plan.drifts,
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
        # Unexpected internal error. We try to record it on the row but the
        # transaction may already be poisoned — best-effort.
        logger.exception(
            "reconcile_ont_internal_error",
            extra={"ont_unit_id": str(ont_unit_id), "error": str(exc)},
        )
        try:
            db.rollback()
        except Exception:
            pass
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
    acs_observed_fallback: AcsObservedFields | None = None,
):
    """Run OLT + ACS reads in parallel.

    Two I/O-bound calls, one each — a small thread pool is the right fit.
    The readers themselves don't share state, so no synchronisation needed.
    """
    if acs_client is None:
        return (
            read_olt_state(olt_adapter, desired, deadline=deadline),
            ReadResult(
                success=True,
                unreachable=False,
                observed=acs_observed_fallback or _absent_acs(),
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
) -> ReconcileResult:
    """Persist state on the OntUnit row + observation table, build the result.

    Called inside the lock context — the caller's transaction commits when
    the context exits normally.
    """
    now = datetime.now(UTC)
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)

    if success and drift_after:
        # Converged everything it was allowed to, but something was withheld.
        # Not `synced` (which asserts zero residual drift) and not a failure
        # either -- no write was rejected. The drift stays visible so the
        # withheld PPP work is discoverable rather than silently absent.
        ont.sync_status = OntSyncStatus.out_of_sync
        ont.last_error = None
    elif success:
        ont.sync_status = OntSyncStatus.synced
        ont.last_error = None
        cleared = clear_acs_delivery_fault(ont)
        if cleared:
            resolve_acs_delivery_fault(
                ont_id=str(ont.id),
                serial_number=str(ont.serial_number or ""),
                before=cleared,
            )
    else:
        ont.sync_status = OntSyncStatus.out_of_sync
        ont.last_error = failure.message if failure else "unknown failure"
        _record_acs_delivery_fault(ont, failure)
    ont.last_reconciled_at = now

    if observed_after is not None:
        upsert_ont_observation(db, ont.id, observed_after)

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

    AUDIT-ONLY: callers use this purely to measure how often a verify mismatch
    is explainable by inform-lag (``would_be_graced``). It does NOT change the
    reconcile outcome — enforcing the grace is a separate, flag-gated step.
    """
    applied_acs_fields = {
        a.field for a in actions_applied if getattr(a, "surface", None) == "acs"
    }
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
