"""Reconciler package for OLT + ACS state.

The reconciler is the only subsystem that writes to OLT (SSH) and ACS
(GenieACS NBI). It enforces ``OntDesiredState`` onto the live OLT and ACS, and
records the result as ``OntObservedState``.

This first commit lands the type system and the validator only. The reader,
planner, applier, and ``reconcile_ont`` entry point follow in subsequent
commits. No production code path yet imports this package.

Public surface:

* ``OntDesiredState``      — what the operator/system wants.
* ``OntObservedState``     — last-seen live values from OLT + ACS.
* ``ReconcileResult``      — outcome of one reconcile pass.
* ``ReconcileFailure``     — failure detail attached to a non-success result.
* ``ReconcileFailureReason`` — string constants enumerating failure modes.
* ``SyncStatus``           — per-ONT enum literal.
* ``validate_desired``     — boundary validation for a proposed mutation.
* ``Validation``           — validation result type.

The action types (OltAction / AcsAction subclasses), the planner, and the
applier are deliberately not re-exported here; they're implementation details
of the reconciler and consumed only within this package.
"""

from .actions import (
    AcsAddObject,
    AcsDeleteObject,
    AcsSetDhcpServer,
    AcsSetIpv6,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetRemoteAccess,
    AcsSetWanIp,
    AcsSetWifiConfig,
    AcsSetWifiPassword,
    AcsSetWifiSsid,
    Action,
    OltAuthorize,
    OltClearIphost,
    OltCreateServicePort,
    OltDeleteServicePort,
    OltIpconfig,
    OltModifyDescription,
    OltModifyLineProfile,
    OltModifyServiceProfile,
    OltOmciInternetConfig,
    OltOmciPppoe,
    OltOmciWanConfig,
    OltReset,
    OltTr069ServerConfig,
)
from .adapters import (
    apply_proposed_change,
    desired_from_ont_unit,
    observed_from_ont_observation,
    upsert_ont_observation,
)
from .applier import (
    ApplyContext,
    ApplyError,
    ApplyResult,
    SecretResolver,
    apply_plan,
    passthrough_secret,
)
from .core import reconcile_ont
from .locking import LockConflict, LockError, OntNotFound, acquire_reconcile_lock
from .planner import AcsIdentity, Plan, compute_plan, resolve_acs_device_id
from .readers import ReadResult, read_acs_state, read_olt_state
from .state import (
    AcsObservedFields,
    AppliedAction,
    Drift,
    ObserveSurface,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
    OntWanProposedChange,
    OntWanProposedField,
    PppoeProvisioningMethod,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileMode,
    ReconcileResult,
    SyncStatus,
    Tr069RemoteAccessParameterPaths,
    Tr069WifiParameterPaths,
    Tr181WanParameterPaths,
    WanMode,
    WriteSurface,
)
from .validator import Validation, validate_desired

__all__ = (
    "AcsAddObject",
    "AcsDeleteObject",
    "AcsIdentity",
    "AcsObservedFields",
    "AcsSetDhcpServer",
    "AcsSetIpv6",
    "AcsSetWanIp",
    "AcsSetWifiConfig",
    "AcsSetManagementServer",
    "AcsSetNatEnabled",
    "AcsSetPppoe",
    "AcsSetRemoteAccess",
    "AcsSetWifiPassword",
    "AcsSetWifiSsid",
    "Action",
    "ApplyContext",
    "ApplyError",
    "ApplyResult",
    "AppliedAction",
    "Drift",
    "LockConflict",
    "LockError",
    "ObserveSurface",
    "OltAuthorize",
    "OltClearIphost",
    "OltCreateServicePort",
    "OltDeleteServicePort",
    "OltIpconfig",
    "OltModifyDescription",
    "OltModifyLineProfile",
    "OltModifyServiceProfile",
    "OltObservedFields",
    "OltOmciInternetConfig",
    "OltOmciPppoe",
    "OltOmciWanConfig",
    "OltReset",
    "OltTr069ServerConfig",
    "OntDesiredState",
    "OntNotFound",
    "OntObservedState",
    "OntWanProposedChange",
    "OntWanProposedField",
    "Plan",
    "PppoeProvisioningMethod",
    "ReadResult",
    "ReconcileFailure",
    "ReconcileFailureReason",
    "ReconcileMode",
    "ReconcileResult",
    "SecretResolver",
    "SyncStatus",
    "Tr069RemoteAccessParameterPaths",
    "Tr069WifiParameterPaths",
    "Tr181WanParameterPaths",
    "Validation",
    "WanMode",
    "WriteSurface",
    "acquire_reconcile_lock",
    "apply_plan",
    "apply_proposed_change",
    "compute_plan",
    "desired_from_ont_unit",
    "observed_from_ont_observation",
    "passthrough_secret",
    "read_acs_state",
    "read_olt_state",
    "reconcile_ont",
    "resolve_acs_device_id",
    "upsert_ont_observation",
    "validate_desired",
)
