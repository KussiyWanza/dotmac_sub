"""Canonical SOT declarations for the scheduler_control_plane domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="scheduler_control_plane",
    services=(
        SOTService(
            name="scheduler.registry",
            module="app.services.scheduler_config",
            owns=(
                "effective scheduled-task registration",
                "registered scheduler boolean control enforcement",
                "registered scheduler cadence and tuning resolution",
                "permanent customer-financial lifecycle task registration",
                "mandatory account-access reconciliation registration",
                "permanent device-projection repair registration",
                "permanent accepted-work drainage registration",
                "event-driven transport exclusion from periodic registration",
                "optional capability task synchronization",
                "Celery runtime schedule config",
            ),
            depends_on=(
                "control.feature_registry",
                "control.settings_spec",
                "runtime.db_sessions",
            ),
            notes=(
                "Scheduler booleans resolve only through canonical feature "
                "controls or registered boolean SettingSpecs. Cadence and "
                "tuning resolve through registered typed SettingSpecs; their "
                "environment variables are bootstrap inputs, not runtime "
                "overrides. Permanent lifecycle, accepted-work drainage, and "
                "projection-repair tasks have no mutable enablement control. "
                "Broker and result-backend URLs remain deployment transport "
                "configuration rather than mutable domain settings. See "
                "docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md."
            ),
        ),
        SOTService(
            name="scheduler.operations",
            module="app.services.scheduler",
            owns=(
                "ScheduledTask cadence management",
                "permanent lifecycle task mutation protection",
                "event-driven transport schedule rejection",
                "manual task enqueue operations",
            ),
            depends_on=("scheduler.registry",),
        ),
        SOTService(
            name="scheduler.worker_control",
            module="app.services.worker_control",
            owns=("worker restart targets", "worker control actions"),
            depends_on=("scheduler.registry",),
        ),
    ),
    entrypoints=("app.tasks.*", "app.web.admin.system", "app.main"),
    rule="Core lifecycle tasks are always registered and cannot be disabled, "
    "renamed, or deleted. This includes customer-financial lifecycle work "
    "durable command dispatch/drainage after acceptance, and canonical "
    "projection repair. Optional capability scheduling composes through "
    "the feature control plane; other mutable scheduler booleans must "
    "have a registered database-authoritative SettingSpec. Ad-hoc "
    "environment/database/default fallback is forbidden for every "
    "registered scheduler scalar. Controls may reject new admission but "
    "cannot freeze accepted work, expiry cleanup, security/session "
    "reconciliation, or canonical projection repair. "
    "Event-driven transports remain requestable but cannot register as "
    "independent periodic repair owners; task bodies remain thin adapters.",
)
