"""Readiness policy for the supported routes mounted by a web worker."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerStartupObservation:
    route_paths: tuple[str, ...]
    preflight_complete: bool


@dataclass(frozen=True, slots=True)
class WorkerReadiness:
    ready: bool
    missing_routes: tuple[str, ...]


def evaluate_worker_readiness(observation: WorkerStartupObservation) -> WorkerReadiness:
    missing = tuple(
        path
        for path in ("/api/v1/subscribers/sync",)
        if path not in observation.route_paths
    )
    return WorkerReadiness(
        ready=observation.preflight_complete and not missing,
        missing_routes=missing,
    )
