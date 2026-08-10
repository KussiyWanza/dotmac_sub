"""ONT provisioning services — direct config application.

RECOMMENDED APPROACH:
    apply_authorization_baseline(db, ont_id)

This function reads config from the source of truth (OntAssignment + OltConfigPack)
and applies it directly to the OLT. The OLT adapter handles idempotency by treating
"already exists" errors as success.

The direct approach:
1. Reads effective config via resolve_effective_ont_config()
2. Creates internet service port (adapter handles "already exists")
3. Executes batched management config (service port, IPHOST, TR-069)
4. No state reconciliation needed - adapter layer is idempotent

Other functions in this module:
- apply_authorization_baseline() — Apply OLT-side internet and ACS reachability
- apply_saved_service_config() — Apply TR-069/CPE service config after bootstrap
- deprovision() — Remove service-ports and return ONT to inventory
- rollback_service_ports() — Cleanup on provisioning failure
- preview_provisioning() — Show what config would be applied
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.network import OntUnit
from app.services.genieacs_client import GenieACSDeliveryCode
from app.services.genieacs_service import genieacs_service
from app.services.network._common import NasTarget
from app.services.network.config_pack_resolution import (
    resolve_effective_config_pack_stage,
)
from app.services.network.effective_ont_config import (
    internet_wcd_index_from_effective_values,
    resolve_effective_ont_config,
)
from app.services.network.huawei_cli_response import project_huawei_result_evidence
from app.services.network.ont_desired_config import set_desired_config_values
from app.services.network.ont_provisioning.context import (
    OltContext as OltContext,
)
from app.services.network.ont_provisioning.context import (
    resolve_olt_context as resolve_olt_context,
)
from app.services.network.ont_provisioning.credentials import (
    mask_credentials as mask_credentials,
)
from app.services.network.ont_provisioning.preflight import (
    validate_prerequisites as validate_prerequisites,
)
from app.services.network.ont_provisioning.result import StepResult as StepResult
from app.services.network.provisioning_events import record_ont_provisioning_event
from app.services.notification_adapter import broadcast_websocket, notify

logger = logging.getLogger(__name__)

# Bootstrap polling constants — configurable via DomainSettings (provisioning domain).
# These module-level variables are kept for backward compatibility with test patching.
# At runtime, use the getter functions which check DomainSettings first.
from app.services.network.provisioning_settings import (
    default_for as _provisioning_default,
)
from app.services.network.provisioning_settings import (
    get_olt_write_mode_enabled,
    get_pppoe_provisioning_method,
    get_tr069_bootstrap_timeout,
)

# From the registered spec, not from a `ProvisioningDefaults` dataclass that
# held a second copy of the same numbers. Still module-level and still only
# DEFAULTS — a configured value reaches the getters above, not these — which is
# the pre-existing shape these constants have always had.
_BOOTSTRAP_TIMEOUT_SEC = _provisioning_default("tr069_bootstrap_timeout_sec")
_BOOTSTRAP_POLL_INTERVAL_SEC = _provisioning_default(
    "tr069_bootstrap_poll_interval_sec"
)
_TR069_TASK_READY_TIMEOUT_SEC = _provisioning_default("tr069_task_ready_timeout_sec")
_TR069_TASK_READY_POLL_INTERVAL_SEC = _provisioning_default(
    "tr069_task_ready_poll_interval_sec"
)
_TR069_BINDING_READBACK_RETRY_DELAY_SEC = 2.0

_QUEUED_ACS_DELIVERY_CODES = frozenset(code.value for code in GenieACSDeliveryCode)


def _is_queued_acs_delivery_code(value: object) -> bool:
    return str(value or "") in _QUEUED_ACS_DELIVERY_CODES


def _result_is_queued_acs_delivery(result: object) -> bool:
    data = getattr(result, "data", None)
    if isinstance(data, dict) and (
        data.get("delivery_status") == "queued"
        or _is_queued_acs_delivery_code(data.get("delivery_code"))
    ):
        return True
    return bool(getattr(result, "waiting", False)) or _is_queued_acs_delivery_code(
        getattr(result, "error_code", None)
    )


def _action_step(name: str, result: object) -> dict[str, object]:
    """Preserve machine-readable delivery state while serializing an action."""
    payload: dict[str, object] = {
        "step": name,
        "success": bool(getattr(result, "success", False)),
        "message": str(getattr(result, "message", "")),
    }
    evidence = project_huawei_result_evidence(result)
    if evidence is not None:
        payload.update(evidence)
    if _result_is_queued_acs_delivery(result):
        payload["waiting"] = True
    return payload


def _step_is_queued_acs_delivery(step: dict[str, object]) -> bool:
    return bool(step.get("waiting")) or _is_queued_acs_delivery_code(
        step.get("error_code")
    )


def _validate_olt_profile_dependencies(
    db: Session,
    *,
    olt_id: str,
    duration_start: float,
) -> StepResult | None:
    """Return a failed StepResult when live OLT profile dependencies are invalid."""
    from app.services.network.olt_dependency_preflight import (
        validate_olt_profile_dependencies,
    )

    result = validate_olt_profile_dependencies(
        db,
        olt_id=olt_id,
        operation="provisioning",
    )
    if result.success:
        return None

    ms = int((time.monotonic() - duration_start) * 1000)
    return StepResult(
        "provision",
        False,
        result.message,
        ms,
        data={"dependency_audit": result.audit},
    )


# ---------------------------------------------------------------------------
# Step completion tracking
# ---------------------------------------------------------------------------


def _record_step(db: Session, ont: OntUnit, step_name: str, result: StepResult) -> None:
    """Log and persist provisioning step completion.

    The event log is append-only for audit/replay. The ONT's provisioning_status
    is updated when the overall workflow completes, not per-step.
    """
    record_ont_provisioning_event(db, ont, step_name, result)
    logger.info(
        "Provisioning step %s for ONT %s: success=%s waiting=%s duration_ms=%s message=%s",
        step_name,
        ont.id,
        result.success,
        result.waiting,
        result.duration_ms,
        result.message[:200],
    )


def _commit_without_expiring(db: Session) -> None:
    """Commit before slow device I/O without forcing ORM reloads afterwards."""
    previous = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = previous


def _set_domain_outcome(
    domain_outcomes: dict[str, dict[str, Any]],
    domain: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"status": status, "message": message}
    if details:
        payload["details"] = details
    domain_outcomes[domain] = payload


def _with_domain_outcomes(
    domain_outcomes: dict[str, dict[str, Any]],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(data or {})
    payload["domain_outcomes"] = dict(sorted(domain_outcomes.items()))
    return payload


def _domain_outcome_status(
    domain_outcomes: dict[str, dict[str, Any]],
    domain: str,
) -> str | None:
    payload = domain_outcomes.get(domain)
    if isinstance(payload, dict):
        status = payload.get("status")
        return str(status) if status is not None else None
    return None


# ---------------------------------------------------------------------------
# NOTE: Individual step functions for manual wizard provisioning have been
# removed. The following functions were deprecated:
#   - create_service_port() - Use provision_with_reconciliation() instead
#   - configure_management_ip() - Use provision_with_reconciliation() instead
#   - activate_internet_config() - Use provision_with_reconciliation() instead
#   - configure_wan_olt() - Use provision_with_reconciliation() instead
#   - bind_tr069() - Use provision_with_reconciliation() instead
#
# All OLT provisioning now uses direct config application:
# provision_with_reconciliation() reads from OntAssignment + OltConfigPack
# (source of truth) and applies config directly. The OLT adapter handles
# idempotency by treating "already exists" errors as success.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SERVICE: Wait for TR-069 bootstrap (GenieACS registration)
# ---------------------------------------------------------------------------


def _is_celery_task_context() -> bool:
    """Check if we're running inside a Celery task.

    Returns True if current execution is within a Celery worker task,
    False if running in a web request or other context.
    """
    try:
        from celery import current_task

        return current_task is not None and current_task.request.id is not None
    except (ImportError, AttributeError):
        return False


def _is_background_context() -> bool:
    """Check if we're in a safe context for blocking operations.

    Returns True if running in Celery task, thread pool, or explicit background context.
    Returns False if running on the main thread (likely a web request handler).
    """
    import threading

    # Check Celery task context first — always safe
    if _is_celery_task_context():
        return True

    # Main thread is NOT safe for blocking (uvicorn/gunicorn web workers)
    # Check this BEFORE pattern matching since "MainThread" contains "thread"
    if threading.current_thread() is threading.main_thread():
        return False

    # Check thread name patterns used by background executors
    # Only reached for non-main threads
    thread_name = threading.current_thread().name.lower()
    background_patterns = (
        "celery",
        "threadpool",  # More specific than "thread"
        "pool",
        "worker",
        "background",
        "executor",
    )
    if any(pattern in thread_name for pattern in background_patterns):
        return True

    # Non-main thread without recognized pattern — assume safe
    # (Better to allow than block legitimate background work)
    return True


class BlockingOperationError(RuntimeError):
    """Raised when a blocking operation is called from a web request context."""

    pass


def _bootstrap_poll_error_result(exc: Exception, duration_ms: int) -> StepResult:
    """Classify stale PostgreSQL poll connections as retryable verification."""
    message = str(exc)
    retryable_db_error = "idle-in-transaction timeout" in message.casefold()
    return StepResult(
        "wait_tr069_bootstrap",
        False,
        f"Bootstrap poll error: {message}",
        duration_ms,
        critical=not retryable_db_error,
        waiting=retryable_db_error,
        data={
            "failure_class": (
                "retryable_db_connection"
                if retryable_db_error
                else "acs_bootstrap_poll_error"
            )
        },
    )


ProgressCallback = Callable[[int, int, str], None]


def wait_tr069_bootstrap(
    db: Session,
    ont_id: str,
    *,
    allow_blocking: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> StepResult:
    """Poll GenieACS until the ONT registers after TR-069 binding.

    This is a SYNCHRONOUS blocking function that polls until the device
    appears in ACS or timeout is reached. Use for immediate feedback flows.

    Uses exponential backoff (2s -> 4s -> 8s -> 16s -> 30s cap) for smarter
    polling that reduces ACS API load by ~40% while still detecting fast
    registrations quickly.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        allow_blocking: Deprecated, kept for compatibility. Always True now.
        progress_callback: Optional callback(attempt, max_attempts, message)
            called on each poll iteration for real-time progress updates.

    Returns:
        StepResult with success=True if device found, False on timeout.

    Note:
        This function blocks for up to 120 seconds (configurable).
        Progress is reported via the callback if provided.
    """
    from app.models.network import OntProvisioningStatus
    from app.services.network.backoff import BOOTSTRAP_BACKOFF, ExponentialBackoff
    from app.services.network.ont_status import set_provisioning_status

    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return StepResult("wait_tr069_bootstrap", False, "ONT not found")

    # Get configurable timeout from DomainSettings (or use defaults)
    bootstrap_timeout = get_tr069_bootstrap_timeout(db)

    try:
        from app.services.network._resolve import resolve_genieacs_with_reason

        # Use exponential backoff: 2s -> 4s -> 8s -> 16s -> 30s (capped)
        backoff = ExponentialBackoff(
            BOOTSTRAP_BACKOFF,
            total_timeout=bootstrap_timeout,
        )

        # Estimate max attempts for progress reporting (approximate)
        max_attempts = max(1, int(bootstrap_timeout / BOOTSTRAP_BACKOFF.initial_delay))

        logger.info(
            "TR-069 bootstrap wait started: ont_id=%s serial=%s timeout_sec=%s backoff=2s->30s",
            ont.id,
            ont.serial_number,
            bootstrap_timeout,
        )

        last_poll_error = ""
        for attempt, delay in backoff:
            progress_msg = f"Waiting for ONT to register with ACS... (attempt {attempt}, next poll in {delay:.1f}s)"

            # Emit progress callback
            if progress_callback:
                try:
                    progress_callback(attempt, max_attempts, progress_msg)
                except Exception as cb_exc:
                    logger.warning("Progress callback error: %s", cb_exc)

            try:
                resolved, reason = resolve_genieacs_with_reason(db, ont)
            except Exception as exc:
                db.rollback()
                last_poll_error = str(exc)
                logger.warning(
                    "TR-069 bootstrap wait poll error: ont_id=%s serial=%s attempt=%s delay=%.1fs error=%s",
                    ont.id,
                    ont.serial_number,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue

            if resolved:
                _client, device_id = resolved
                logger.info(
                    "TR-069 bootstrap complete: ont_id=%s serial=%s genieacs_device_id=%s attempts=%s elapsed_ms=%d",
                    ont.id,
                    ont.serial_number,
                    device_id,
                    attempt,
                    int(backoff.elapsed() * 1000),
                )
                # Emit success via callback
                if progress_callback:
                    try:
                        progress_callback(
                            attempt, max_attempts, "Device registered in ACS"
                        )
                    except Exception:
                        pass
                ms = int((time.monotonic() - t0) * 1000)
                result = StepResult(
                    "wait_tr069_bootstrap", True, "Device registered in ACS", ms
                )
                _record_step(db, ont, "wait_tr069_bootstrap", result)
                return result

            logger.info(
                "TR-069 bootstrap wait poll miss: ont_id=%s serial=%s attempt=%s delay=%.1fs reason=%s",
                ont.id,
                ont.serial_number,
                attempt,
                delay,
                reason,
            )
            db.rollback()
            time.sleep(delay)

        # Backoff exhausted (timeout reached)
        logger.warning(
            "TR-069 bootstrap timeout: ont_id=%s serial=%s timeout_sec=%s",
            ont.id,
            ont.serial_number,
            bootstrap_timeout,
        )
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "wait_tr069_bootstrap",
            False,
            (
                f"Device not found in ACS after {bootstrap_timeout}s"
                if not last_poll_error
                else f"Device not found in ACS after {bootstrap_timeout}s; last poll error: {last_poll_error}"
            ),
            ms,
            critical=False,
            waiting=True,
            data={"failure_class": "acs_bootstrap_timeout"},
        )
        set_provisioning_status(
            ont, OntProvisioningStatus.pending_acs_registration, strict=False
        )
        _record_step(db, ont, "wait_tr069_bootstrap", result)
        return result
    except Exception as e:
        logger.error("Error during TR-069 bootstrap poll: %s", e)
        try:
            db.rollback()
        except Exception:
            db.invalidate()
        ms = int((time.monotonic() - t0) * 1000)
        result = _bootstrap_poll_error_result(e, ms)
        try:
            _record_step(db, ont, "wait_tr069_bootstrap", result)
        except Exception:
            logger.warning(
                "Failed to record wait_tr069_bootstrap step after bootstrap poll error",
                exc_info=True,
            )
        return result


def _decrypt_optional_secret(value: object) -> str | None:
    if value in (None, ""):
        return None
    from app.services.credential_crypto import decrypt_credential

    try:
        return decrypt_credential(str(value)) or str(value)
    except Exception:
        return str(value)


def _igd_wan_instance_for_vlan_from_snapshot(
    ont: OntUnit, wan_vlan: int | None
) -> int | None:
    capabilities = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(capabilities, dict):
        return None
    capabilities = capabilities.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    wan_caps = capabilities.get("wan")
    if not isinstance(wan_caps, dict):
        return None
    if wan_caps.get("data_model") != "InternetGatewayDevice":
        return None
    connections = wan_caps.get("connections")
    if not isinstance(connections, list):
        return None
    requested_vlan = str(wan_vlan or "").strip()
    for require_vlan_match in (True, False):
        for item in connections:
            if not isinstance(item, dict):
                continue
            service = str(item.get("detected_wan_service") or "").upper()
            vlan = str(item.get("detected_wan_vlan") or "").strip()
            if service == "TR069":
                continue
            if require_vlan_match and requested_vlan and vlan == requested_vlan:
                return int(item.get("index") or 0) or None
            if not require_vlan_match and not requested_vlan:
                return int(item.get("index") or 0) or None
    return None


# Phases that represent state actually applied to a device. Preparatory phases
# (config-pack resolution, prerequisite validation, state reads) succeeding
# says nothing about whether the customer got service, so they must not make a
# wholly failed run look partial.
_DEVICE_APPLY_PHASES = frozenset(
    {
        "olt_provisioning",
        "post_authorization_acs_link",
        "internet_l2",
        "management_and_omci_apply",
        "stale_wan_cleanup",
    }
)


def _any_device_phase_succeeded(phase_timings: list[dict[str, Any]]) -> bool:
    """Return True when at least one phase that writes to a device succeeded.

    Used to distinguish a partial run from a total failure. Phases that do not
    report a ``success`` key are ignored rather than assumed successful, and
    ``olt_provisioning`` carries its inner steps as ``subphases``.
    """

    def _succeeded(entry: object) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("phase") in _DEVICE_APPLY_PHASES
            and entry.get("success") is True
        )

    for entry in phase_timings:
        if _succeeded(entry):
            return True
        if isinstance(entry, dict) and any(
            _succeeded(sub) for sub in entry.get("subphases") or ()
        ):
            return True
    return False


def _wcd_index_or_none(value: object) -> int | None:
    """Coerce a WANConnectionDevice index to a positive int, else None."""
    try:
        index = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return index if index > 0 else None


def _igd_management_wcd_indexes(ont: OntUnit) -> set[int]:
    """Return WANConnectionDevice indexes the snapshot reports as TR-069 management.

    A PPPoE service must never be written into the container that carries the
    ACS management connection: doing so severs the only path we have to the
    device. The snapshot labels those connections ``TR069``.
    """
    indexes: set[int] = set()
    capabilities = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(capabilities, dict):
        return indexes
    capabilities = capabilities.get("capabilities")
    if not isinstance(capabilities, dict):
        return indexes
    wan_caps = capabilities.get("wan")
    if not isinstance(wan_caps, dict):
        return indexes
    connections = wan_caps.get("connections")
    if not isinstance(connections, list):
        return indexes
    for item in connections:
        if not isinstance(item, dict):
            continue
        if str(item.get("detected_wan_service") or "").upper() != "TR069":
            continue
        index = _wcd_index_or_none(item.get("index"))
        if index:
            indexes.add(index)
    return indexes


def _resolve_igd_pppoe_wcd_index(
    ont: OntUnit,
    effective_values: dict[str, Any],
    wan_vlan: int | None,
) -> int | None:
    """Resolve the TR-098 WANConnectionDevice that PPPoE belongs in.

    Returns ``None`` when no safe container can be identified. Callers must
    fail closed on ``None`` rather than defaulting to an index: index 1 is the
    management container on a freshly commissioned ONT, and writing PPPoE
    there is refused downstream with a message that reads like stale device
    config and has historically misdirected operators into a factory reset.
    """
    management = _igd_management_wcd_indexes(ont)

    snapshot_index = _igd_wan_instance_for_vlan_from_snapshot(ont, wan_vlan)
    if snapshot_index and snapshot_index not in management:
        return snapshot_index

    # The device may not expose an internet container yet (nothing has been
    # provisioned into it). Fall back to the OLT config pack's declared
    # pppoe_wcd_index, but only when it is explicitly configured.
    configured = internet_wcd_index_from_effective_values(effective_values, default=0)
    if configured and configured not in management:
        return configured

    return None


def _mark_ppp_wan_requires_precreated(ont: OntUnit) -> None:
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        return
    snapshot = dict(snapshot)
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, dict):
        return
    capabilities = dict(capabilities)
    pending = capabilities.get("pending_actions")
    if isinstance(pending, dict):
        pending = dict(pending)
        pending.pop("add_ppp_wan", None)
        if pending:
            capabilities["pending_actions"] = pending
        else:
            capabilities.pop("pending_actions", None)
    wan_caps = capabilities.get("wan")
    if isinstance(wan_caps, dict):
        wan_caps = dict(wan_caps)
        wan_caps["supports_tr069_add_ppp_wan"] = False
        wan_caps["supports_tr069_set_ppp_credentials"] = False
        wan_caps["requires_precreated_ppp_wan"] = True
        capabilities["wan"] = wan_caps
    snapshot["capabilities"] = capabilities
    ont.tr069_last_snapshot = snapshot
    flag_modified(ont, "tr069_last_snapshot")


def _provision_wan_service_instances(
    db: Session,
    ont_id: str,
    *,
    ont: OntUnit | None = None,
    effective_values: dict | None = None,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    """Provision WAN from resolved desired_config only.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ont: Optional pre-fetched ONT to avoid redundant lookup.
        effective_values: Optional pre-resolved config values to avoid redundant resolution.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.ont_action_wan import (
        set_pppoe_credentials,
        set_wan_dhcp,
        set_wan_static,
    )

    if ont is None:
        ont = db.get(OntUnit, ont_id)
    if not ont:
        return [], [], ["ONT not found"]

    if effective_values is None:
        effective = resolve_effective_ont_config(db, ont)
        effective_values = (
            effective.get("values", {}) if isinstance(effective, dict) else {}
        )
    wan_mode = str(effective_values.get("wan_mode") or "").strip().lower()
    wan_vlan = effective_values.get("wan_vlan")
    wan_vlan_int = int(wan_vlan) if wan_vlan not in (None, "") else None
    raw_instance_index = effective_values.get("wan_instance_index")
    raw_pppoe_wcd_index = effective_values.get("pppoe_wcd_index")
    instance_index = (
        int(raw_instance_index)
        if raw_instance_index not in (None, "")
        else int(raw_pppoe_wcd_index)
        if raw_pppoe_wcd_index not in (None, "")
        else None
    )
    steps: list[dict[str, object]] = []
    needs_input: list[str] = []
    hard_failures: list[str] = []

    if not wan_mode:
        needs_input.append("WAN mode is not configured in desired_config.")
        return steps, needs_input, hard_failures
    if wan_mode == "bridge":
        steps.append(
            {
                "step": "provision_wan_desired_config",
                "success": True,
                "message": "Bridge WAN is configured on the OLT service port.",
            }
        )
        return steps, needs_input, hard_failures
    if instance_index is None:
        needs_input.append(
            "WAN instance index is not configured in desired_config or OLT config pack."
        )
        return steps, needs_input, hard_failures

    pppoe_provisioning_method = get_pppoe_provisioning_method(db)
    wan_provisioning_mode = str(
        effective_values.get("wan_provisioning_mode") or "omci_wan_config"
    )
    omci_wan_supported = (
        wan_provisioning_mode == "omci_wan_config"
        and effective_values.get("internet_config_ip_index") is not None
        and effective_values.get("wan_config_profile_id") is not None
    )

    if (
        wan_mode == "pppoe"
        and pppoe_provisioning_method != "tr069"
        and omci_wan_supported
    ):
        omci_vlan = effective_values.get("pppoe_omci_vlan") or wan_vlan_int
        pppoe_username = effective_values.get("pppoe_username")
        pppoe_password = _decrypt_optional_secret(
            effective_values.get("pppoe_password")
        )
        if not pppoe_username or not pppoe_password:
            needs_input.append(
                "PPPoE credentials are not configured in desired_config."
            )
            return steps, needs_input, hard_failures
        if omci_vlan is not None and get_olt_write_mode_enabled(db):
            ctx, err = resolve_olt_context(db, ont_id)
            if ctx is None:
                hard_failures.append(f"resolve_olt_context: {err}")
                return steps, needs_input, hard_failures
            adapter = get_protocol_adapter(ctx.olt)
            ip_index = int(effective_values.get("internet_config_ip_index"))
            pppoe_result = adapter.configure_pppoe(
                ctx.fsp,
                ctx.olt_ont_id,
                ip_index=ip_index,
                vlan_id=int(omci_vlan),
                priority=int(effective_values.get("wan_priority") or 0),
                username=str(pppoe_username),
                password=str(pppoe_password),
            )
            steps.append(
                {
                    "step": "configure_pppoe_omci:desired_config",
                    "success": pppoe_result.success,
                    "message": pppoe_result.message,
                }
            )
            if not pppoe_result.success and pppoe_provisioning_method == "omci":
                hard_failures.append(f"configure_pppoe_omci: {pppoe_result.message}")
                return steps, needs_input, hard_failures
            if not pppoe_result.success:
                # auto mode: OMCI write failed; we'll fall through to TR-069 below.
                # adapter.configure_pppoe may have partially written OMCI state before
                # failing — clear the IPHOST at this ip_index so the TR-069 PPPoE push
                # isn't fighting a stale OMCI dialer (the BOI TechSquad 2026-04-17
                # dup-auth scenario).
                clear_result = adapter.clear_iphost_config(
                    ctx.fsp, ctx.olt_ont_id, ip_index=ip_index
                )
                steps.append(
                    {
                        "step": "rollback_pppoe_omci_partial:desired_config",
                        "success": clear_result.success,
                        "message": clear_result.message,
                    }
                )
            if pppoe_result.success:
                inet_result = adapter.configure_internet_config(
                    ctx.fsp,
                    ctx.olt_ont_id,
                    ip_index=ip_index,
                )
                steps.append(
                    {
                        "step": "internet_config_olt:desired_config",
                        "success": inet_result.success,
                        "message": inet_result.message,
                    }
                )
                if not inet_result.success and pppoe_provisioning_method == "omci":
                    hard_failures.append(f"internet_config_olt: {inet_result.message}")
                    return steps, needs_input, hard_failures
                wan_profile_id = effective_values.get("wan_config_profile_id")
                if inet_result.success and wan_profile_id is not None:
                    wan_result = adapter.configure_wan_config(
                        ctx.fsp,
                        ctx.olt_ont_id,
                        ip_index=ip_index,
                        profile_id=int(wan_profile_id),
                    )
                    steps.append(
                        {
                            "step": "configure_wan_olt:desired_config",
                            "success": wan_result.success,
                            "message": wan_result.message,
                        }
                    )
                    if not wan_result.success and pppoe_provisioning_method == "omci":
                        hard_failures.append(f"configure_wan_olt: {wan_result.message}")
                        return steps, needs_input, hard_failures
                return steps, needs_input, hard_failures
            if pppoe_provisioning_method == "omci":
                hard_failures.append(f"configure_pppoe_omci: {pppoe_result.message}")
                return steps, needs_input, hard_failures
    elif (
        wan_mode == "pppoe"
        and pppoe_provisioning_method == "omci"
        and not omci_wan_supported
    ):
        steps.append(
            {
                "step": "configure_pppoe_omci:skipped",
                "success": True,
                "message": (
                    f"OLT WAN provisioning mode {wan_provisioning_mode} does not "
                    "support wan-config/internet-config; falling back to DotMac ACS."
                ),
            }
        )

    detected_index = instance_index
    if (
        wan_mode == "pppoe"
        and getattr(ont, "tr069_data_model", None) == "InternetGatewayDevice"
    ):
        # Never fall back to a bare index here. On a freshly commissioned ONT
        # the only container that exists is the TR-069 management one, and
        # defaulting to it produced a downstream refusal ("the selected
        # WANConnectionDevice is not empty") that reads like stale device
        # config and has misdirected operators toward a factory reset, which
        # would sever ONT management.
        detected_index = _resolve_igd_pppoe_wcd_index(
            ont, effective_values, wan_vlan_int
        )
        if detected_index is None:
            message = (
                f"No safe WANConnectionDevice exists for PPPoE VLAN {wan_vlan_int}. "
                "The ONT must expose or precreate an internet PPP WAN container before TR-069 can push credentials."
            )
            steps.append(
                {
                    "step": "provision_wan_desired_config",
                    "success": False,
                    "message": message,
                }
            )
            hard_failures.append(f"provision_wan_desired_config: {message}")
            _mark_ppp_wan_requires_precreated(ont)
            return steps, needs_input, hard_failures

    if wan_mode == "pppoe":
        pppoe_username = effective_values.get("pppoe_username")
        pppoe_password = _decrypt_optional_secret(
            effective_values.get("pppoe_password")
        )
        if not pppoe_username or not pppoe_password:
            needs_input.append(
                "PPPoE credentials are not configured in desired_config."
            )
            return steps, needs_input, hard_failures
        result = set_pppoe_credentials(
            db,
            ont_id,
            username=str(pppoe_username),
            password=str(pppoe_password),
            instance_index=detected_index,
            wan_vlan=wan_vlan_int,
        )
    elif wan_mode == "dhcp":
        result = set_wan_dhcp(
            db,
            ont_id,
            instance_index=detected_index,
            wan_vlan=wan_vlan_int,
        )
    elif wan_mode in {"static", "static_ip"}:
        ip_address = effective_values.get("wan_static_ip")
        subnet = effective_values.get("wan_static_subnet")
        gateway = effective_values.get("wan_static_gateway")
        if not (ip_address and subnet and gateway):
            needs_input.append(
                "Static WAN IP, subnet, and gateway are required in desired_config."
            )
            return steps, needs_input, hard_failures
        result = set_wan_static(
            db,
            ont_id,
            ip_address=str(ip_address),
            subnet_mask=str(subnet),
            gateway=str(gateway),
            dns_servers=str(effective_values.get("wan_static_dns") or "") or None,
            instance_index=detected_index,
        )
    else:
        needs_input.append(f"Unsupported WAN mode in desired_config: {wan_mode}")
        return steps, needs_input, hard_failures

    steps.append(_action_step("provision_wan_desired_config", result))
    if not result.success and not _result_is_queued_acs_delivery(result):
        hard_failures.append(f"provision_wan_desired_config: {result.message}")
    return steps, needs_input, hard_failures


def apply_saved_service_config(
    db: Session,
    ont_id: str,
    *,
    effective_config: dict | None = None,
) -> StepResult:
    """Apply saved TR-069 service intent once the ONT is visible in ACS.

    WAN service provisioning is instance-backed. ONT-level desired settings are
    read from ``OntUnit.desired_config`` via ``resolve_effective_ont_config``.

    Missing operator inputs are reported in ``data["needs_input"]`` and fail the
    ACS apply step so the end-to-end provisioning result is not marked complete
    before the intended service config can be pushed.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        effective_config: Optional pre-resolved config to avoid redundant resolution.
    """

    from app.models.network import OntProvisioningStatus
    from app.services.credential_crypto import decrypt_credential
    from app.services.network.ont_action_network import (
        probe_wan_capabilities,
    )
    from app.services.network.ont_status import set_provisioning_status

    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return StepResult("apply_saved_service_config", False, "ONT not found")
    acs = genieacs_service

    # Use pre-resolved config if provided, otherwise resolve once
    if effective_config is not None:
        resolved = effective_config
    else:
        resolved = resolve_effective_ont_config(db, ont)
    effective_values = resolved.get("values", {})

    steps: list[dict[str, object]] = []
    needs_input: list[str] = []
    hard_failures: list[str] = []
    pending_deliveries: list[str] = []

    def _append(name: str, result) -> None:
        success = bool(getattr(result, "success", False))
        message = str(getattr(result, "message", ""))
        waiting = not success and _result_is_queued_acs_delivery(result)
        step = _action_step(name, result)
        step["waiting"] = waiting
        steps.append(step)
        if not success:
            detail = f"{name}: {message}"
            if waiting:
                pending_deliveries.append(detail)
            else:
                hard_failures.append(detail)

    cr_username = effective_values.get("cr_username")
    cr_password = _decrypt_optional_secret(effective_values.get("cr_password"))
    if cr_username and cr_password:
        _append(
            "set_connection_request_credentials",
            acs.set_connection_request_credentials(
                db,
                ont_id,
                str(cr_username),
                str(cr_password),
            ),
        )
    elif effective_values.get("tr069_acs_server_id"):
        needs_input.append(
            "Connection request credentials are incomplete in desired_config/OLT defaults."
        )

    if effective_values.get("wan_mode"):
        probe_result = probe_wan_capabilities(db, ont_id)
        _append("probe_wan_capabilities", probe_result)

        wan_instance_steps, wan_instance_needs, wan_instance_failures = (
            _provision_wan_service_instances(
                db, ont_id, ont=ont, effective_values=effective_values
            )
        )
        for step in wan_instance_steps:
            if not step.get("success") and _step_is_queued_acs_delivery(step):
                step["waiting"] = True
                pending_deliveries.append(f"{step.get('step')}: {step.get('message')}")
            steps.append(step)
        needs_input.extend(wan_instance_needs)
        hard_failures.extend(wan_instance_failures)
        logger.info(
            "ONT %s: Provisioned %d WAN desired-config steps",
            ont.serial_number,
            len(wan_instance_steps),
        )

    lan_values = {
        "lan_ip": effective_values.get("lan_ip"),
        "lan_subnet": effective_values.get("lan_subnet"),
        "dhcp_enabled": effective_values.get("lan_dhcp_enabled"),
        "dhcp_start": effective_values.get("lan_dhcp_start"),
        "dhcp_end": effective_values.get("lan_dhcp_end"),
    }
    # On customer turn-ups, push DHCP server enable defensively unless the operator
    # explicitly disabled it. Some ONT firmware ships with DHCP off, leaving the
    # customer with WiFi associated but no LAN IPs handed out.
    if effective_values.get("wan_mode"):
        dhcp_enabled_value = lan_values.get("dhcp_enabled")
        if not isinstance(dhcp_enabled_value, bool):
            dhcp_enabled_value = True
        _append(
            "configure_lan_tr069",
            acs.set_lan_config(
                db,
                ont_id,
                lan_ip=str(lan_values.get("lan_ip") or "") or None,
                lan_subnet=str(lan_values.get("lan_subnet") or "") or None,
                dhcp_enabled=dhcp_enabled_value,
                dhcp_start=str(lan_values.get("dhcp_start") or "") or None,
                dhcp_end=str(lan_values.get("dhcp_end") or "") or None,
            ),
        )

    # Reuse already-resolved effective_values (no second resolution needed)
    wifi_password = None
    raw_wifi_password = effective_values.get("wifi_password")
    if raw_wifi_password:
        try:
            wifi_password = decrypt_credential(str(raw_wifi_password))
        except ValueError:
            wifi_password = str(raw_wifi_password)
    channel = effective_values.get("wifi_channel")
    try:
        channel_int = int(str(channel).strip()) if channel not in (None, "") else None
    except (TypeError, ValueError):
        channel_int = None
        needs_input.append("WiFi channel must be numeric.")
    wifi_values = {
        "enabled": effective_values.get("wifi_enabled")
        if effective_values.get("wifi_enabled") is not None
        else None,
        "ssid": effective_values.get("wifi_ssid"),
        "password": wifi_password,
        "channel": channel_int,
        "security_mode": effective_values.get("wifi_security_mode"),
    }
    if any(value not in (None, "", []) for value in wifi_values.values()):
        wifi_enabled_value = wifi_values.get("enabled")
        _append(
            "configure_wifi_tr069",
            acs.set_wifi_config(
                db,
                ont_id,
                enabled=wifi_enabled_value
                if isinstance(wifi_enabled_value, bool)
                else None,
                ssid=str(wifi_values.get("ssid") or "") or None,
                password=str(wifi_values.get("password") or "") or None,
                channel=channel_int,
                security_mode=str(wifi_values.get("security_mode") or "") or None,
            ),
        )

    ms = int((time.monotonic() - t0) * 1000)
    if hard_failures:
        set_provisioning_status(ont, OntProvisioningStatus.failed, strict=False)
        db.add(ont)
        return StepResult(
            "apply_saved_service_config",
            False,
            "; ".join(hard_failures),
            ms,
            critical=False,
            waiting=False,  # Sync-only: no waiting semantics
            data={"steps": steps, "needs_input": needs_input},
        )
    if pending_deliveries:
        set_provisioning_status(
            ont, OntProvisioningStatus.pending_service_config, strict=False
        )
        db.add(ont)
        return StepResult(
            "apply_saved_service_config",
            False,
            "Saved ONT service config is queued and awaiting the next Inform.",
            ms,
            critical=False,
            waiting=True,
            data={
                "steps": steps,
                "needs_input": needs_input,
                "pending_deliveries": pending_deliveries,
            },
        )
    if not steps and not needs_input:
        set_desired_config_values(ont, {"delivery.pending_apply": None})
        set_provisioning_status(ont, OntProvisioningStatus.provisioned, strict=False)
        db.add(ont)
        return StepResult(
            "apply_saved_service_config",
            True,
            "No saved ONT service config to apply.",
            ms,
            critical=False,
            skipped=True,
            data={"steps": steps},
        )
    message = "Saved ONT service config applied."
    if needs_input:
        message = "Saved ONT service config is incomplete."
    if not needs_input:
        set_desired_config_values(ont, {"delivery.pending_apply": None})
        set_provisioning_status(ont, OntProvisioningStatus.provisioned, strict=False)
    else:
        set_provisioning_status(
            ont, OntProvisioningStatus.pending_service_config, strict=False
        )
    db.add(ont)
    return StepResult(
        "apply_saved_service_config",
        not needs_input,
        message,
        ms,
        critical=False,
        waiting=False,  # Sync-only: no waiting semantics
        data={"steps": steps, "needs_input": needs_input},
    )


# ---------------------------------------------------------------------------
# NOTE: Additional deprecated step functions:
#   - set_connection_request_credentials() - Applied via apply_saved_service_config()
#   - push_pppoe_omci() - Applied via _provision_wan_service_instances()
#
# These are now called internally from apply_saved_service_config() which
# reads config from the source of truth (OntAssignment + OltConfigPack).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SERVICE: Ensure NAS VLAN (MikroTik)
# ---------------------------------------------------------------------------


def ensure_nas_vlan(
    db: Session,
    *,
    nas: NasTarget,
    vlan_id: int,
    parent_interface: str = "ether3",
    ip_address: str,
    pppoe_service_name: str | None = None,
    pppoe_default_profile: str = "default",
) -> StepResult:
    """Create VLAN interface + IP + PPPoE server on a NAS device.

    Idempotent — reuses existing VLAN if it matches.

    Args:
        db: Database session.
        nas: Lightweight DTO describing the target NAS device. Callers that
            hold a ``NasDevice`` ORM row should construct a :class:`NasTarget`
            from it before calling this function.
        vlan_id: VLAN ID to create.
        parent_interface: Physical interface (default "ether3").
        ip_address: IP address with CIDR for the VLAN interface.
        pppoe_service_name: Optional PPPoE service name.
        pppoe_default_profile: PPP profile name (default "default").
    """
    t0 = time.monotonic()

    try:
        from app.services.nas._mikrotik_vlan import provision_vlan_full

        # ``provision_vlan_full`` is typed to ``NasDevice`` but only reads the
        # attributes present on ``NasTarget``; the DTO is duck-type compatible.
        vlan_result = provision_vlan_full(
            cast(Any, nas),
            vlan_id=vlan_id,
            parent_interface=parent_interface,
            ip_address=ip_address,
            pppoe_service_name=pppoe_service_name,
            pppoe_default_profile=pppoe_default_profile,
        )
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "ensure_nas_vlan", vlan_result.success, vlan_result.message, ms
        )
    except Exception as exc:
        logger.error("NAS VLAN provisioning failed: %s", exc)
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "ensure_nas_vlan", False, f"NAS VLAN provisioning failed: {exc}", ms
        )


# ---------------------------------------------------------------------------
# SERVICE: Rollback service ports
# ---------------------------------------------------------------------------


def deprovision(
    db: Session,
    ont_id: str,
) -> StepResult:
    """Full deprovision: remove service-ports, deauthorize, clear DB state.

    Delegates to the existing ``return_to_inventory`` service which handles
    the correct sequence: service-ports → OLT registration → DB cleanup.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
    """
    t0 = time.monotonic()
    try:
        from app.services.network.ont_inventory import return_ont_to_inventory
    except ImportError:
        logger.warning(
            "ont_inventory module not available — skipping inventory return for ONT %s",
            ont_id,
        )
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "deprovision",
            True,
            "Deprovision skipped: ont_inventory module not available",
            ms,
        )

    action_result = return_ont_to_inventory(db, ont_id)
    ms = int((time.monotonic() - t0) * 1000)
    return StepResult("deprovision", action_result.success, action_result.message, ms)


def download_firmware(
    db: Session,
    ont_id: str,
    *,
    firmware_image_id: str,
) -> StepResult:
    """Trigger an ACS Download RPC for an ONT firmware image."""
    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return StepResult("download_firmware", False, "ONT not found")

    result = genieacs_service.firmware_upgrade(db, ont_id, firmware_image_id)
    ms = int((time.monotonic() - t0) * 1000)
    step_result = StepResult(
        "download_firmware",
        bool(result.success),
        result.message,
        ms,
        critical=False,
        data=result.data,
    )
    _record_step(db, ont, "download_firmware", step_result)
    return step_result


def rollback_service_ports(
    db: Session,
    ont_id: str,
) -> StepResult:
    """Remove all service-ports for an ONT from the OLT.

    Used for cleanup after failed provisioning.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
    """
    from app.services.network.imported_service_ports import (
        ImportedServicePortStateMissing,
        delete_imported_service_port,
        list_imported_service_ports,
        require_imported_service_port_state,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("rollback_service_ports", False, err)

    adapter = get_protocol_adapter(ctx.olt)
    try:
        require_imported_service_port_state(db, olt_id=ctx.olt.id)
        ports = list_imported_service_ports(
            db,
            olt_id=ctx.olt.id,
            fsp=ctx.fsp,
            ont_id_on_olt=ctx.olt_ont_id,
        )
    except ImportedServicePortStateMissing as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult("rollback_service_ports", False, str(exc), ms)
    if not ports:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports", True, "No service ports to remove", ms
        )

    deleted = 0
    errors = 0
    for port in ports:
        delete_result = adapter.delete_service_port(port.index)
        if delete_result.success:
            delete_imported_service_port(
                db,
                olt_id=ctx.olt.id,
                port_index=port.index,
            )
            deleted += 1
        else:
            errors += 1
            logger.warning(
                "Rollback: failed to delete service-port %d: %s",
                port.index,
                delete_result.message,
            )

    ms = int((time.monotonic() - t0) * 1000)
    message = f"Removed {deleted} service-port(s)"
    if errors:
        message += f", {errors} failed"
    result = StepResult("rollback_service_ports", errors == 0, message, ms)
    _record_step(db, ctx.ont, "rollback_service_ports", result)
    return result


def rollback_service_port_indices(
    db: Session,
    ont_id: str,
    *,
    port_indices: list[int],
    expected_olt_id: str | None = None,
) -> StepResult:
    """Remove only the specified service-port indices for an ONT.

    This is the safer rollback path for watchdog retries because it deletes only
    the ports created by the original failed operation and refuses to act if the
    ONT has moved to another OLT.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("rollback_service_ports", False, err)

    if expected_olt_id is not None and str(ctx.olt.id) != str(expected_olt_id):
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports",
            False,
            "ONT placement changed since rollback failure; refusing targeted retry.",
            ms,
        )

    normalized_indices = sorted({int(index) for index in port_indices})
    if not normalized_indices:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports",
            False,
            "No targeted service-port indices recorded for retry.",
            ms,
        )

    adapter = get_protocol_adapter(ctx.olt)
    deleted = 0
    errors = 0
    error_messages: list[str] = []
    for port_index in normalized_indices:
        delete_result = adapter.delete_service_port(port_index)
        if delete_result.success:
            deleted += 1
        else:
            errors += 1
            error_messages.append(f"{port_index}: {delete_result.message}")
            logger.warning(
                "Targeted rollback: failed to delete service-port %d: %s",
                port_index,
                delete_result.message,
            )

    ms = int((time.monotonic() - t0) * 1000)
    message = f"Removed {deleted} targeted service-port(s)"
    if errors:
        message += f", {errors} failed"
    result = StepResult(
        "rollback_service_ports",
        errors == 0,
        message,
        ms,
        data={
            "targeted_port_indices": normalized_indices,
            "errors": error_messages,
        },
    )
    _record_step(db, ctx.ont, "rollback_service_ports", result)
    return result


# Profile resolution, preflight validation, and command preview live in
# app.services.network.ont_provisioning.* and are imported above for compatibility.


def _ensure_static_management_ip_from_profile(
    db: Session,
    ont: OntUnit,
    profile: object | None,
) -> tuple[bool, str]:
    """Reserve a management IP before building static IPHOST desired state."""
    if profile is None:
        return True, "No provisioning profile selected."
    mode = getattr(profile, "mgmt_ip_mode", None)
    mode_value = getattr(mode, "value", mode)
    if mode_value != "static_ip":
        return True, "Management IP mode is not static."
    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    # Source of truth is the effective config (OntAssignment + OltConfigPack)
    effective_mgmt_ip = effective_values.get("mgmt_ip_address")
    if effective_mgmt_ip:
        return True, "Static management IP already assigned."

    pool_id = getattr(profile, "mgmt_ip_pool_id", None)
    if not pool_id:
        return False, "Static management IP mode requires a management IP pool."

    from app.services.network.ont_management_ipam import allocate_ont_management_ip

    try:
        allocation = allocate_ont_management_ip(db, ont=ont, pool_id=pool_id)
    except ValueError as exc:
        return False, str(exc)
    return True, f"Reserved static management IP {allocation.address}."


def _ensure_authorization_management_ip(
    db: Session,
    ctx: OltContext,
    values: dict[str, Any],
) -> tuple[bool, str, bool]:
    """Reserve OLT-pool management IP before building authorization IPHOST.

    Authorization baseline needs deterministic ACS reachability. If the ONT
    already has static management values, keep them. If the operator explicitly
    selected DHCP/inactive, respect that. Otherwise, use the OLT's management
    pool when one is configured.
    """
    if not values.get("mgmt_vlan"):
        return True, "No management VLAN configured.", False

    if all(
        values.get(key) for key in ("mgmt_ip_address", "mgmt_subnet", "mgmt_gateway")
    ):
        return True, "Static management IP already assigned.", False

    mgmt_ip_mode = str(values.get("mgmt_ip_mode") or "").strip().lower()
    if mgmt_ip_mode in {"dhcp", "inactive"}:
        return True, "Management IP mode explicitly uses DHCP/inactive.", False

    pool_id = getattr(ctx.olt, "mgmt_ip_pool_id", None)
    if not pool_id:
        if mgmt_ip_mode in {"static", "static_ip"}:
            return (
                False,
                "Static management IP mode requires an OLT management IP pool.",
                False,
            )
        return True, "No OLT management IP pool configured; using DHCP IPHOST.", False

    from app.services.network.ont_management_ipam import allocate_ont_management_ip

    try:
        allocation = allocate_ont_management_ip(
            db,
            ont=ctx.ont,
            olt=ctx.olt,
            pool_id=pool_id,
        )
    except ValueError as exc:
        return False, str(exc), False

    message = (
        f"Reused management IP {allocation.address}."
        if allocation.reused
        else f"Reserved management IP {allocation.address}."
    )
    return True, message, True


# ---------------------------------------------------------------------------
# AUTHORIZATION BASELINE / DIRECT CONFIG APPLICATION
# ---------------------------------------------------------------------------


def apply_authorization_baseline(
    db: Session,
    ont_id: str,
    *,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
    effective_config: dict | None = None,
) -> StepResult:
    """Apply the OLT-side baseline required after ONT authorization.

    The baseline owns network plumbing: internet service-port/VLAN/GEM mapping,
    management service-port/IPHOST, WAN OLT binding, and TR-069 profile binding.
    Customer CPE settings such as PPPoE credentials, WiFi, and LAN settings stay
    in ``apply_saved_service_config`` and can be pushed from the UI after ACS
    visibility.
    """
    from app.models.network import OntProvisioningStatus
    from app.services.network.ont_status import set_provisioning_status

    t0 = time.monotonic()
    phase_started = t0
    phase_timings: list[dict[str, Any]] = []

    def _record_phase(name: str, **details: Any) -> None:
        nonlocal phase_started
        now = time.monotonic()
        phase_timings.append(
            {
                "phase": name,
                "duration_ms": int((now - phase_started) * 1000),
                **details,
            }
        )
        phase_started = now

    try:
        ont = db.get(OntUnit, ont_id)
    except Exception:
        ont = None
    if ont is None:
        return StepResult("authorization_baseline", False, "ONT not found")

    effective_config, config_pack_result = resolve_effective_config_pack_stage(
        db,
        ont,
        effective_config=effective_config,
    )
    _record_phase("config_pack_resolution", success=config_pack_result.success)
    baseline_domain_outcomes: dict[str, dict[str, Any]] = {}
    _set_domain_outcome(
        baseline_domain_outcomes,
        "config_pack_resolution",
        "succeeded" if config_pack_result.success else "terminal_failure",
        config_pack_result.message,
    )
    if not dry_run:
        _record_step(db, ont, config_pack_result.step_name, config_pack_result)
        db.flush()
    if not config_pack_result.success:
        result = StepResult(
            "authorization_baseline",
            False,
            config_pack_result.message,
            duration_ms=int((time.monotonic() - t0) * 1000),
            data={
                "config_pack_resolution": config_pack_result.data,
                "domain_outcomes": baseline_domain_outcomes,
                "phase_timings": phase_timings,
            },
        )
        if not dry_run:
            set_provisioning_status(ont, OntProvisioningStatus.failed, strict=False)
            db.flush()
            _record_step(db, ont, "authorization_baseline", result)
        return result

    if not dry_run:
        preflight = validate_prerequisites(
            db,
            ont_id,
            ont=ont,
            effective_config=effective_config,
        )
        _record_phase(
            "prerequisite_validation",
            success=bool(preflight.get("ready_to_provision", preflight.get("ready"))),
        )
        if not bool(preflight.get("ready_to_provision", preflight.get("ready"))):
            failed_checks = [
                check
                for check in preflight.get("checks", [])
                if check.get("status") == "fail"
            ]
            message = "Authorization baseline blocked: prerequisites are incomplete."
            if failed_checks:
                message = f"{message} {failed_checks[0].get('message') or ''}".strip()
            result = StepResult(
                "authorization_baseline",
                False,
                message,
                duration_ms=int((time.monotonic() - t0) * 1000),
                data={
                    "config_pack_resolution": config_pack_result.data,
                    "domain_outcomes": baseline_domain_outcomes,
                    "checks": preflight.get("checks", []),
                    "failed_checks": failed_checks,
                    "phase_timings": phase_timings,
                },
            )
            set_provisioning_status(ont, OntProvisioningStatus.failed, strict=False)
            db.flush()
            _record_step(db, ont, "authorization_baseline", result)
            return result

        set_provisioning_status(ont, OntProvisioningStatus.partial, strict=False)
        db.flush()
        _commit_without_expiring(db)

    _record_phase("pre_provision_state")
    phase_started = time.monotonic()
    result = provision_with_reconciliation(
        db,
        ont_id,
        dry_run=dry_run,
        allow_low_optical_margin=allow_low_optical_margin,
        effective_config=effective_config,
    )
    _record_phase(
        "olt_provisioning",
        success=result.success,
        subphases=(result.data or {}).get("phase_timings", []),
    )
    baseline_result = StepResult(
        "authorization_baseline",
        result.success,
        result.message,
        result.duration_ms,
        critical=result.critical,
        skipped=result.skipped,
        waiting=result.waiting,
        data={
            **(result.data or {}),
            "config_pack_resolution": config_pack_result.data,
            "phase_timings": phase_timings,
        },
    )
    baseline_domain_outcomes.update(
        dict((baseline_result.data or {}).get("domain_outcomes", {}))
    )
    if baseline_result.success and not dry_run:
        _set_domain_outcome(
            baseline_domain_outcomes,
            "acs_bootstrap_verify",
            "pending_verification",
            "Waiting for ACS bootstrap verification after baseline apply.",
        )
        baseline_result.waiting = True
        if not baseline_result.message:
            baseline_result.message = "Authorization baseline applied; waiting for ACS bootstrap verification."
    baseline_result.data = {
        **(baseline_result.data or {}),
        "domain_outcomes": dict(sorted(baseline_domain_outcomes.items())),
    }

    if not dry_run and baseline_result.success:
        phase_started = time.monotonic()
        try:
            from app.services.network._resolve import reconcile_ont_tr069_device

            linked_device, link_reason = reconcile_ont_tr069_device(db, ont)
            link_result = StepResult(
                "post_authorization_acs_link",
                linked_device is not None,
                (
                    f"Linked ACS device: {link_reason}"
                    if linked_device is not None
                    else f"ACS device not linked yet: {link_reason}"
                ),
                duration_ms=int((time.monotonic() - t0) * 1000),
                critical=False,
                data={
                    "tr069_device_id": (
                        str(getattr(linked_device, "id", ""))
                        if linked_device is not None
                        else None
                    ),
                    "genieacs_device_id": (
                        getattr(linked_device, "genieacs_device_id", None)
                        if linked_device is not None
                        else None
                    ),
                    "reason": link_reason,
                },
            )
            _record_step(db, ont, "post_authorization_acs_link", link_result)
            if result.data:
                baseline_result.data = {
                    **baseline_result.data,
                    "post_authorization_acs_link": link_result.data,
                }
            else:
                baseline_result.data = {
                    **(baseline_result.data or {}),
                    "post_authorization_acs_link": link_result.data,
                }
            _record_phase("post_authorization_acs_link", success=link_result.success)
        except Exception as exc:
            logger.warning(
                "Post-authorization ACS reconciliation failed for ONT %s: %s",
                ont.id,
                exc,
                exc_info=True,
            )
            link_result = StepResult(
                "post_authorization_acs_link",
                False,
                f"ACS reconciliation failed: {exc}",
                duration_ms=int((time.monotonic() - t0) * 1000),
                critical=False,
            )
            _record_step(db, ont, "post_authorization_acs_link", link_result)
            _record_phase("post_authorization_acs_link", success=False)

    if not dry_run:
        if baseline_result.success:
            if (
                _domain_outcome_status(baseline_domain_outcomes, "acs_bootstrap_verify")
                == "pending_verification"
            ):
                final_status = OntProvisioningStatus.pending_acs_registration
            else:
                final_status = OntProvisioningStatus.provisioned
        else:
            # A run that landed some phases is not the same as one that landed
            # none. Reporting a blanket "failed" for a customer whose OLT-side
            # provisioning succeeded (and who may be carrying traffic) inverts
            # triage: the field reads as down while the service is up.
            final_status = (
                OntProvisioningStatus.partial
                if _any_device_phase_succeeded(phase_timings)
                else OntProvisioningStatus.failed
            )
        set_provisioning_status(
            ont,
            final_status,
            strict=False,
        )
        db.flush()

    _record_phase("finalize_baseline_state", success=baseline_result.success)
    baseline_result.duration_ms = int((time.monotonic() - t0) * 1000)
    baseline_result.data = {
        **(baseline_result.data or {}),
        "phase_timings": phase_timings,
    }
    return baseline_result


def provision_with_reconciliation(
    db: Session,
    ont_id: str,
    *,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
    effective_config: dict | None = None,
    skip_dependency_check: bool = False,
) -> StepResult:
    """Provision an ONT by applying config directly. Adapter handles idempotency.

    This simplified approach:
    1. Reads effective config via resolve_effective_ont_config()
    2. Creates internet service port (adapter treats "already exists" as success)
    3. Executes batched management config (mgmt port, IPHOST, TR-069)

    No state reconciliation needed - the OLT adapter layer is idempotent.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        dry_run: If True, show what would be configured without executing.
        allow_low_optical_margin: Ignored (kept for API compatibility).
        effective_config: Optional pre-resolved config to avoid redundant resolution.
        skip_dependency_check: Skip OLT dependency validation (if already done by caller).

    Returns:
        StepResult with provisioning outcome.
    """
    from app.services.network.iphost_priority import resolve_management_iphost_priority
    from app.services.network.olt_batched_mgmt import (
        create_batched_mgmt_spec_from_config_pack,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    phase_started = t0
    phase_timings: list[dict[str, Any]] = []

    def _record_phase(name: str, **details: Any) -> None:
        nonlocal phase_started
        now = time.monotonic()
        phase_timings.append(
            {
                "phase": name,
                "duration_ms": int((now - phase_started) * 1000),
                **details,
            }
        )
        phase_started = now

    # Get context
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("provision", False, err)

    # Use pre-resolved config if provided, otherwise resolve
    if effective_config is not None:
        effective = effective_config
    else:
        effective = resolve_effective_ont_config(db, ctx.ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    config_pack = effective.get("config_pack")

    if not config_pack:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult("provision", False, "OLT config pack not found", ms)

    logger.info(
        "Starting provisioning for ONT %s serial=%s olt=%s fsp=%s",
        ont_id,
        ctx.ont.serial_number,
        ctx.olt.name,
        ctx.fsp,
    )

    # Build preview data
    wan_vlan = values.get("wan_vlan")
    raw_wan_gem = values.get("wan_gem_index")
    raw_wan_mode = str(values.get("wan_mode") or "").strip().lower()
    wan_mode = (
        "bridge"
        if raw_wan_mode in {"bridge", "bridged", "setup_via_onu"}
        else raw_wan_mode
    )
    mgmt_vlan = values.get("mgmt_vlan")
    tr069_profile = values.get("tr069_olt_profile_id")

    if wan_vlan and raw_wan_gem is None:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "provision",
            False,
            "WAN VLAN is configured but internet GEM index is missing",
            ms,
        )
    wan_gem = int(raw_wan_gem) if raw_wan_gem is not None else None

    if dry_run:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "provision",
            True,
            "Dry run: showing config to apply",
            ms,
            data={
                "dry_run": True,
                "wan_vlan": wan_vlan,
                "wan_gem_index": wan_gem,
                "wan_mode": wan_mode,
                "bridge_native_vlan": int(wan_vlan)
                if wan_vlan and wan_mode == "bridge"
                else None,
                "mgmt_vlan": mgmt_vlan,
                "tr069_profile_id": tr069_profile,
                "mgmt_ip_mode": values.get("mgmt_ip_mode"),
            },
        )

    # Skip dependency validation if caller already validated (e.g., orchestrator preflight)
    if not skip_dependency_check:
        olt_id = getattr(ctx.olt, "id", None)
        if olt_id is None:
            ms = int((time.monotonic() - t0) * 1000)
            return StepResult(
                "provision", False, "OLT ID not available for dependency audit", ms
            )
        dependency_failure = _validate_olt_profile_dependencies(
            db,
            olt_id=str(olt_id),
            duration_start=t0,
        )
        if dependency_failure is not None:
            _record_step(db, ctx.ont, "provision", dependency_failure)
            _send_failure_notification(ctx, ont_id, dependency_failure)
            return dependency_failure

    adapter = get_protocol_adapter(ctx.olt)
    steps_completed: list[str] = []
    created_port_indices: list[int] = []
    domain_outcomes: dict[str, dict[str, Any]] = {}
    command_timings: list[dict[str, Any]] = []
    _record_phase("prepare")

    def _run_olt_command(name: str, command: Callable[[], Any]) -> Any:
        command_started = time.monotonic()
        try:
            command_result = command()
        except Exception as exc:
            command_timings.append(
                {
                    "command": name,
                    "success": False,
                    "duration_ms": int((time.monotonic() - command_started) * 1000),
                    "error_type": type(exc).__name__,
                }
            )
            raise
        timing: dict[str, Any] = {
            "command": name,
            "success": bool(getattr(command_result, "success", False)),
            "duration_ms": int((time.monotonic() - command_started) * 1000),
        }
        evidence = project_huawei_result_evidence(command_result)
        if evidence is not None:
            timing.update(evidence)
        command_timings.append(timing)
        return command_result

    # 1. Create internet service port (adapter returns success on "already exists")
    _commit_without_expiring(db)
    if wan_vlan:
        result = _run_olt_command(
            "create_internet_service_port",
            lambda: adapter.create_service_port(
                ctx.fsp,
                ctx.olt_ont_id,
                gem_index=int(wan_gem),
                vlan_id=int(wan_vlan),
            ),
        )
        if not result.success:
            _record_phase("internet_l2", success=False)
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                f"Internet service port failed: {result.message}",
                ms,
                data=_with_domain_outcomes(
                    {
                        "olt_l2_apply": {
                            "status": "terminal_failure",
                            "message": f"Internet service port failed: {result.message}",
                        }
                    },
                    {
                        "command_timings": command_timings,
                        "phase_timings": phase_timings,
                    },
                ),
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        steps_completed.append(f"service_port_vlan_{wan_vlan}")
        if result.data and result.data.get("service_port_index"):
            created_port_indices.append(result.data["service_port_index"])

        if wan_mode == "bridge":
            native_result = _run_olt_command(
                "configure_bridge_native_vlan",
                lambda: adapter.configure_port_native_vlan(
                    ctx.fsp,
                    ctx.olt_ont_id,
                    eth_port=1,
                    vlan_id=int(wan_vlan),
                ),
            )
            if not native_result.success:
                for port_idx in created_port_indices:
                    try:
                        _run_olt_command(
                            "rollback_internet_service_port",
                            lambda port_idx=port_idx: adapter.delete_service_port(
                                port_idx
                            ),
                        )
                    except Exception:
                        pass
                ms = int((time.monotonic() - t0) * 1000)
                step_result = StepResult(
                    "provision",
                    False,
                    f"Bridge native VLAN failed: {native_result.message}",
                    ms,
                    data=_with_domain_outcomes(
                        {
                            "olt_l2_apply": {
                                "status": "terminal_failure",
                                "message": f"Bridge native VLAN failed: {native_result.message}",
                            }
                        },
                        {
                            "rollback_performed": len(created_port_indices) > 0,
                            "command_timings": command_timings,
                        },
                    ),
                )
                _record_step(db, ctx.ont, "provision", step_result)
                _send_failure_notification(ctx, ont_id, step_result)
                return step_result
            steps_completed.append(f"native_vlan_{wan_vlan}_eth_1")
        _set_domain_outcome(
            domain_outcomes,
            "olt_l2_apply",
            "succeeded",
            "Internet L2 apply completed.",
            details={"wan_vlan": wan_vlan, "wan_gem_index": wan_gem},
        )
    else:
        _set_domain_outcome(
            domain_outcomes,
            "olt_l2_apply",
            "succeeded",
            "No internet L2 apply required for this provisioning run.",
        )
    _record_phase("internet_l2", success=True)

    # 2. Clear any stale WAN config before applying new configuration (best-effort)
    # Huawei OLTs retain ipconfig/internet-config/wan-config after deauthorization or from
    # factory defaults, which can cause ip-index mismatch errors on new provisioning.
    # IPHOST is also cleared here so reuse-registration paths self-heal stale entries
    # at ip_index slots the new baseline won't itself rewrite.
    stale_cleared = []
    _commit_without_expiring(db)
    for ip_index in (0, 1):
        result = _run_olt_command(
            f"clear_iphost_{ip_index}",
            lambda ip_index=ip_index: adapter.clear_iphost_config(
                ctx.fsp, ctx.olt_ont_id, ip_index=ip_index
            ),
        )
        if result.success:
            stale_cleared.append(f"iphost:{ip_index}")
        result = _run_olt_command(
            f"clear_internet_config_{ip_index}",
            lambda ip_index=ip_index: adapter.clear_internet_config(
                ctx.fsp, ctx.olt_ont_id, ip_index=ip_index
            ),
        )
        if result.success:
            stale_cleared.append(f"internet-config:{ip_index}")
        result = _run_olt_command(
            f"clear_wan_config_{ip_index}",
            lambda ip_index=ip_index: adapter.clear_wan_config(
                ctx.fsp, ctx.olt_ont_id, ip_index=ip_index
            ),
        )
        if result.success:
            stale_cleared.append(f"wan-config:{ip_index}")
    if stale_cleared:
        steps_completed.append(f"cleared_stale_wan_config({','.join(stale_cleared)})")
        logger.info(
            "Cleared stale WAN config for ONT %s on %s: %s",
            ctx.ont.serial_number,
            ctx.olt.name,
            stale_cleared,
        )
    _record_phase("stale_wan_cleanup")

    # 3. Execute batched management config (mgmt port, IPHOST, internet-config, wan-config, TR-069)
    # mgmt_vlan is only legitimately absent in pure bridge mode. For any routed/NAT
    # turn-up, a missing mgmt VLAN means the ONT will come online but never reach the
    # ACS — fail explicitly instead of silently returning success.
    if not mgmt_vlan and wan_mode != "bridge":
        ms = int((time.monotonic() - t0) * 1000)
        step_result = StepResult(
            "provision",
            False,
            "Management VLAN not resolved (config pack incomplete?)",
            ms,
            data=_with_domain_outcomes(
                {
                    **domain_outcomes,
                    "management_path_apply": {
                        "status": "terminal_failure",
                        "message": "Management VLAN not resolved (config pack incomplete?)",
                    },
                },
                {"command_timings": command_timings},
            ),
        )
        _record_step(db, ctx.ont, "provision", step_result)
        _send_failure_notification(ctx, ont_id, step_result)
        return step_result
    if mgmt_vlan:
        ok, mgmt_message, allocated_mgmt_ip = _ensure_authorization_management_ip(
            db,
            ctx,
            values,
        )
        if not ok:
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                mgmt_message,
                ms,
                data=_with_domain_outcomes(
                    {
                        **domain_outcomes,
                        "management_path_apply": {
                            "status": "terminal_failure",
                            "message": mgmt_message,
                        },
                    },
                    {"command_timings": command_timings},
                ),
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        if allocated_mgmt_ip:
            db.flush()
            _commit_without_expiring(db)
            effective = resolve_effective_ont_config(db, ctx.ont)
            values = effective.get("values", {}) if isinstance(effective, dict) else {}
            config_pack = effective.get("config_pack")
            steps_completed.append("reserved_management_ip")
            logger.info(
                "Reserved ONT management IP during authorization baseline for %s: %s",
                ctx.ont.serial_number,
                mgmt_message,
            )
            if not config_pack:
                ms = int((time.monotonic() - t0) * 1000)
                step_result = StepResult(
                    "provision",
                    False,
                    "OLT config pack not found after management IP allocation",
                    ms,
                    data=_with_domain_outcomes(
                        {
                            **domain_outcomes,
                            "management_path_apply": {
                                "status": "terminal_failure",
                                "message": "OLT config pack not found after management IP allocation",
                            },
                        },
                        {"command_timings": command_timings},
                    ),
                )
                _record_step(db, ctx.ont, "provision", step_result)
                _send_failure_notification(ctx, ont_id, step_result)
                return step_result

        raw_mgmt_gem_index = values.get("mgmt_gem_index")
        mgmt_priority = resolve_management_iphost_priority(
            db,
            olt_id=ctx.olt.id,
            fsp=ctx.fsp,
            ont_id_on_olt=ctx.olt_ont_id,
            mgmt_vlan_tag=mgmt_vlan,
            mgmt_gem_index=raw_mgmt_gem_index,
            line_profile_id=values.get("authorization_line_profile_id"),
        )
        mgmt_ip_mode = str(values.get("mgmt_ip_mode") or "").strip().lower()
        has_static_mgmt_ip = all(
            values.get(key)
            for key in ("mgmt_ip_address", "mgmt_subnet", "mgmt_gateway")
        )
        if mgmt_priority is None and (
            mgmt_ip_mode in {"static", "static_ip"} or has_static_mgmt_ip
        ):
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                (
                    "Management IPHOST priority could not be resolved from "
                    "imported OLT state"
                ),
                ms,
                data=_with_domain_outcomes(
                    {
                        **domain_outcomes,
                        "management_path_apply": {
                            "status": "terminal_failure",
                            "message": (
                                "Management IPHOST priority could not be resolved from "
                                "imported OLT state"
                            ),
                        },
                    },
                    {"command_timings": command_timings},
                ),
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        mgmt_spec = create_batched_mgmt_spec_from_config_pack(
            config_pack,
            ctx.fsp,
            ctx.olt_ont_id,
            mgmt_gem_index=(
                int(raw_mgmt_gem_index) if raw_mgmt_gem_index is not None else None
            ),
            allocated_ip=values.get("mgmt_ip_address"),
            subnet_mask=values.get("mgmt_subnet"),
            gateway=values.get("mgmt_gateway"),
            ip_priority=mgmt_priority,
            internet_config_ip_index=values.get("internet_config_ip_index"),
            wan_config_profile_id=values.get("wan_config_profile_id"),
            tr069_profile_id=values.get("tr069_olt_profile_id"),
        )
        _commit_without_expiring(db)
        result = _run_olt_command(
            "configure_management_batch",
            lambda: adapter.configure_management_batch(mgmt_spec),
        )
        if not result.success:
            # Rollback created ports on failure
            for port_idx in created_port_indices:
                try:
                    _run_olt_command(
                        "rollback_internet_service_port",
                        lambda port_idx=port_idx: adapter.delete_service_port(port_idx),
                    )
                except Exception:
                    pass
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                f"Management config failed: {result.message}",
                ms,
                data=_with_domain_outcomes(
                    {
                        **domain_outcomes,
                        "management_path_apply": {
                            "status": "terminal_failure",
                            "message": f"Management config failed: {result.message}",
                        },
                    },
                    {
                        "rollback_performed": len(created_port_indices) > 0,
                        "command_timings": command_timings,
                    },
                ),
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        steps_completed.extend(result.data.get("steps_completed", []))
        completed_batch_steps = result.data.get("steps_completed", [])
        failed_batch_steps = result.data.get("steps_failed", [])
        if any(
            str(step).startswith(("create_mgmt_service_port", "configure_iphost"))
            for step in completed_batch_steps
        ):
            _set_domain_outcome(
                domain_outcomes,
                "management_path_apply",
                "succeeded",
                "Management path apply completed.",
            )
        if any(str(step).startswith("bind_tr069") for step in completed_batch_steps):
            _set_domain_outcome(
                domain_outcomes,
                "tr069_bind_apply",
                "succeeded",
                "TR-069 profile bind apply completed.",
            )
        elif tr069_profile is not None:
            _set_domain_outcome(
                domain_outcomes,
                "tr069_bind_apply",
                "retryable_failure",
                "TR-069 profile bind did not complete during management apply.",
            )
        else:
            _set_domain_outcome(
                domain_outcomes,
                "tr069_bind_apply",
                "succeeded",
                "No TR-069 bind apply required for this provisioning run.",
            )
        if any(
            str(step).startswith(("activate_internet_config", "configure_wan"))
            for step in failed_batch_steps
        ):
            _set_domain_outcome(
                domain_outcomes,
                "omci_wan_apply",
                "retryable_failure",
                "One or more OMCI WAN apply steps failed after management-path apply.",
            )
        elif any(
            str(step).startswith(("activate_internet_config", "configure_wan"))
            for step in completed_batch_steps
        ):
            _set_domain_outcome(
                domain_outcomes,
                "omci_wan_apply",
                "succeeded",
                "OMCI WAN apply completed.",
            )
        else:
            _set_domain_outcome(
                domain_outcomes,
                "omci_wan_apply",
                "succeeded",
                "No OMCI WAN apply required for this provisioning run.",
            )
    else:
        _set_domain_outcome(
            domain_outcomes,
            "management_path_apply",
            "succeeded",
            "Bridge mode does not require management-path apply.",
        )
        _set_domain_outcome(
            domain_outcomes,
            "tr069_bind_apply",
            "succeeded",
            "No TR-069 bind apply required for this provisioning run.",
        )
        _set_domain_outcome(
            domain_outcomes,
            "omci_wan_apply",
            "succeeded",
            "No OMCI WAN apply required for this provisioning run.",
        )
    _record_phase("management_and_omci_apply")

    readback_port_indices: list[int] = []
    service_ports_result = None
    try:
        _commit_without_expiring(db)
        service_ports_result = _run_olt_command(
            "readback_service_ports",
            lambda: adapter.get_service_ports_for_ont(ctx.fsp, ctx.olt_ont_id),
        )
        if service_ports_result.success:
            from app.services.network.imported_service_ports import (
                upsert_imported_service_port_from_readback,
            )

            for port in service_ports_result.data.get("service_ports", []):
                upsert_imported_service_port_from_readback(
                    db,
                    olt=ctx.olt,
                    ont=ctx.ont,
                    port=port,
                    source="provisioning_readback",
                )
                readback_port_indices.append(port.index)
            if readback_port_indices:
                db.flush()
                logger.info(
                    "Imported %d service-port readback row(s) for ONT %s: %s",
                    len(readback_port_indices),
                    ctx.ont.serial_number,
                    readback_port_indices,
                )
        else:
            logger.warning(
                "Service-port readback after provisioning failed for ONT %s: %s",
                ctx.ont.serial_number,
                service_ports_result.message,
            )
    except Exception:
        logger.exception(
            "Failed to import service-port readback after provisioning for ONT %s",
            ctx.ont.serial_number,
        )
    if service_ports_result is not None and not service_ports_result.success:
        _set_domain_outcome(
            domain_outcomes,
            "olt_or_omci_readback_verify",
            "retryable_failure",
            f"Service-port readback failed: {service_ports_result.message}",
        )
    _record_phase(
        "service_port_readback",
        success=bool(service_ports_result and service_ports_result.success),
    )

    expected_tr069_profile = values.get("tr069_olt_profile_id")
    readback_tr069_profile_id: int | None = None
    if expected_tr069_profile is not None:
        try:
            expected_tr069_profile_int = int(expected_tr069_profile)
        except (TypeError, ValueError):
            expected_tr069_profile_int = None
        if expected_tr069_profile_int is None:
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                f"Invalid TR-069 profile id in config pack: {expected_tr069_profile}",
                ms,
                data=_with_domain_outcomes(
                    {
                        **domain_outcomes,
                        "olt_or_omci_readback_verify": {
                            "status": "terminal_failure",
                            "message": (
                                "Invalid TR-069 profile id in config pack: "
                                f"{expected_tr069_profile}"
                            ),
                        },
                    },
                    {
                        "steps_completed": steps_completed,
                        "created_service_port_indices": created_port_indices,
                        "readback_service_port_indices": readback_port_indices,
                        "command_timings": command_timings,
                    },
                ),
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result

        try:
            _commit_without_expiring(db)
            tr069_binding_result = _run_olt_command(
                "readback_tr069_profile_binding",
                lambda: adapter.get_tr069_profile_binding(ctx.fsp, ctx.olt_ont_id),
            )
        except Exception as exc:
            logger.exception(
                "Failed to read TR-069 profile binding after provisioning for ONT %s",
                ctx.ont.serial_number,
            )
            tr069_binding_result = None

        binding_data = (
            getattr(tr069_binding_result, "data", None)
            if tr069_binding_result is not None
            else None
        )
        if isinstance(binding_data, dict):
            raw_profile_id = binding_data.get("profile_id")
            try:
                readback_tr069_profile_id = (
                    int(raw_profile_id) if raw_profile_id is not None else None
                )
            except (TypeError, ValueError):
                readback_tr069_profile_id = None
        binding_success = bool(
            getattr(tr069_binding_result, "success", False)
            if tr069_binding_result is not None
            else False
        )
        needs_tr069_reboot_retry = (
            "bind_tr069" in steps_completed
            and readback_tr069_profile_id != expected_tr069_profile_int
        )
        if needs_tr069_reboot_retry:
            reboot_result = _run_olt_command(
                "reboot_after_tr069_bind",
                lambda: adapter.reboot_ont(ctx.fsp, ctx.olt_ont_id),
            )
            if reboot_result.success:
                steps_completed.append("reset_after_tr069_bind")
                time.sleep(_TR069_BINDING_READBACK_RETRY_DELAY_SEC)
                try:
                    _commit_without_expiring(db)
                    tr069_binding_result = _run_olt_command(
                        "readback_tr069_profile_binding_after_reboot",
                        lambda: adapter.get_tr069_profile_binding(
                            ctx.fsp, ctx.olt_ont_id
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to re-read TR-069 profile binding after reset for ONT %s",
                        ctx.ont.serial_number,
                    )
                    tr069_binding_result = None

                binding_data = (
                    getattr(tr069_binding_result, "data", None)
                    if tr069_binding_result is not None
                    else None
                )
                if isinstance(binding_data, dict):
                    raw_profile_id = binding_data.get("profile_id")
                    try:
                        readback_tr069_profile_id = (
                            int(raw_profile_id) if raw_profile_id is not None else None
                        )
                    except (TypeError, ValueError):
                        readback_tr069_profile_id = None
                binding_success = bool(
                    getattr(tr069_binding_result, "success", False)
                    if tr069_binding_result is not None
                    else False
                )
            else:
                logger.warning(
                    "TR-069 binding readback retry reset failed for ONT %s: %s",
                    ctx.ont.serial_number,
                    reboot_result.message,
                )

        if (
            not binding_success
            or readback_tr069_profile_id != expected_tr069_profile_int
        ):
            message = (
                getattr(tr069_binding_result, "message", "")
                if tr069_binding_result is not None
                else "TR-069 binding readback failed"
            )
            readback_message = (
                "TR-069 profile binding readback failed: "
                f"expected profile {expected_tr069_profile_int}, "
                f"found {readback_tr069_profile_id}. {message}"
            ).strip()
            tr069_bind_applied = (
                _domain_outcome_status(domain_outcomes, "tr069_bind_apply")
                == "succeeded"
            )
            ms = int((time.monotonic() - t0) * 1000)
            verification_status = (
                "pending_verification" if tr069_bind_applied else "retryable_failure"
            )
            step_result = StepResult(
                "provision",
                tr069_bind_applied,
                (
                    "Provisioning apply completed; waiting for TR-069 binding verification. "
                    f"{readback_message}"
                    if tr069_bind_applied
                    else readback_message
                ),
                ms,
                critical=not tr069_bind_applied,
                waiting=tr069_bind_applied,
                data=_with_domain_outcomes(
                    {
                        **domain_outcomes,
                        "olt_or_omci_readback_verify": {
                            "status": verification_status,
                            "message": readback_message,
                        },
                        "acs_bootstrap_verify": {
                            "status": "pending_verification",
                            "message": "Waiting for ACS bootstrap after TR-069 bind apply.",
                        },
                    }
                    if tr069_bind_applied
                    else {
                        **domain_outcomes,
                        "olt_or_omci_readback_verify": {
                            "status": verification_status,
                            "message": readback_message,
                        },
                    },
                    {
                        "failure_class": "tr069_binding_readback_miss",
                        "steps_completed": steps_completed,
                        "created_service_port_indices": created_port_indices,
                        "readback_service_port_indices": readback_port_indices,
                        "expected_tr069_profile_id": expected_tr069_profile_int,
                        "readback_tr069_profile_id": readback_tr069_profile_id,
                        "command_timings": command_timings,
                    },
                ),
            )
            _record_step(db, ctx.ont, "provision", step_result)
            if not tr069_bind_applied:
                _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        steps_completed.append(f"tr069_profile_{readback_tr069_profile_id}_verified")

    _record_phase(
        "tr069_binding_readback",
        success=(
            expected_tr069_profile is None
            or readback_tr069_profile_id == expected_tr069_profile_int
        ),
    )

    if "olt_or_omci_readback_verify" not in domain_outcomes:
        _set_domain_outcome(
            domain_outcomes,
            "olt_or_omci_readback_verify",
            "succeeded",
            "OLT/OMCI readback verification completed.",
        )

    ms = int((time.monotonic() - t0) * 1000)
    step_result = StepResult(
        "provision",
        True,
        f"Provisioned: {len(steps_completed)} step(s)",
        ms,
        data=_with_domain_outcomes(
            domain_outcomes,
            {
                "steps_completed": steps_completed,
                "created_service_port_indices": created_port_indices,
                "readback_service_port_indices": readback_port_indices,
                "readback_tr069_profile_id": readback_tr069_profile_id,
                "command_timings": command_timings,
                "phase_timings": phase_timings,
            },
        ),
    )
    _record_step(db, ctx.ont, "provision", step_result)

    # Send success notification
    try:
        olt_name = getattr(ctx.olt, "name", None) or "OLT"
        broadcast_websocket(
            event_type="ont_provisioning_success",
            title="ONT Provisioning Successful",
            message=f"ONT {ctx.ont.serial_number} provisioned on {olt_name} port {ctx.fsp}",
            metadata={
                "ont_id": ont_id,
                "serial_number": ctx.ont.serial_number,
                "olt_name": olt_name,
                "fsp": ctx.fsp,
                "duration_ms": step_result.duration_ms,
                "steps_completed": len(steps_completed),
            },
        )
    except Exception as notify_exc:
        logger.warning(
            "Failed to send provisioning notification for ONT %s: %s",
            ctx.ont.serial_number,
            notify_exc,
        )

    return step_result


def _send_failure_notification(
    ctx: OltContext, ont_id: str, result: StepResult
) -> None:
    """Send failure notification to operators."""
    try:
        olt_name = getattr(ctx.olt, "name", None) or "OLT"
        notify.alert_operators(
            title="ONT Provisioning Failed",
            message=f"ONT {ctx.ont.serial_number} provisioning failed on {olt_name}: {result.message}",
            severity="error",
            metadata={
                "ont_id": ont_id,
                "serial_number": ctx.ont.serial_number,
                "olt_name": olt_name,
                "fsp": ctx.fsp,
            },
        )
    except Exception as notify_exc:
        logger.warning(
            "Failed to send failure notification for ONT %s: %s",
            ctx.ont.serial_number,
            notify_exc,
        )


def preview_reconciliation(
    db: Session,
    ont_id: str,
) -> dict:
    """Preview what provisioning would configure (no OLT state reading).

    Args:
        db: Database session.
        ont_id: OntUnit primary key.

    Returns:
        Dictionary with config that would be applied.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return {"error": "ONT not found", "has_changes": False, "is_valid": False}

    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return {"error": err, "has_changes": False, "is_valid": False}

    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    config_pack = effective.get("config_pack")

    if not config_pack:
        return {
            "error": "OLT config pack not found",
            "has_changes": False,
            "is_valid": False,
        }

    wan_vlan = values.get("wan_vlan")
    raw_wan_mode = str(values.get("wan_mode") or "").strip().lower()
    wan_mode = (
        "bridge"
        if raw_wan_mode in {"bridge", "bridged", "setup_via_onu"}
        else raw_wan_mode
    )
    mgmt_vlan = values.get("mgmt_vlan")

    # Build service port list
    service_ports = []
    if wan_vlan:
        wan_gem_index = values.get("wan_gem_index")
        if wan_gem_index is None:
            return {
                "error": "WAN VLAN is configured but internet GEM index is missing",
                "has_changes": False,
                "is_valid": False,
            }
        service_ports.append(
            {
                "purpose": "internet",
                "vlan_id": int(wan_vlan),
                "gem_index": int(wan_gem_index),
            }
        )
    if mgmt_vlan:
        mgmt_gem_index = values.get("mgmt_gem_index")
        if mgmt_gem_index is None:
            return {
                "error": "Management VLAN is configured but management GEM index is missing",
                "has_changes": False,
                "is_valid": False,
            }
        service_ports.append(
            {
                "purpose": "management",
                "vlan_id": int(mgmt_vlan),
                "gem_index": int(mgmt_gem_index),
            }
        )

    return {
        "error": None,
        "has_changes": len(service_ports) > 0,
        "is_valid": True,
        "service_ports": service_ports,
        "bridge_native_vlan": {
            "eth_port": 1,
            "vlan_id": int(wan_vlan),
        }
        if wan_vlan and wan_mode == "bridge"
        else None,
        "management": {
            "vlan": mgmt_vlan,
            "ip_mode": values.get("mgmt_ip_mode"),
            "ip_address": values.get("mgmt_ip_address"),
        }
        if mgmt_vlan
        else None,
        "tr069": {
            "profile_id": values.get("tr069_olt_profile_id"),
            "acs_server_id": values.get("tr069_acs_server_id"),
        }
        if values.get("tr069_olt_profile_id")
        else None,
        "internet_config_ip_index": values.get("internet_config_ip_index"),
        "wan_config_profile_id": values.get("wan_config_profile_id"),
    }
