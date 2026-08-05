"""Canonical SOT declarations for the secrets_credentials domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="secrets_credentials",
    services=(
        SOTService(
            name="secrets.reference_store",
            module="app.services.secrets",
            owns=(
                "secret reference parsing and resolution",
                "OpenBao read/write boundary",
                "bounded secret cache lifecycle",
            ),
        ),
        SOTService(
            name="secrets.settings_policy",
            module="app.services.domain_settings",
            owns=(
                "secret setting classification",
                "secret setting reference persistence",
            ),
            depends_on=("secrets.reference_store",),
        ),
        SOTService(
            name="secrets.credential_crypto",
            module="app.services.credential_crypto",
            owns=(
                "database credential encryption",
                "credential field inventory",
                "current and previous decryption key resolution",
            ),
            depends_on=("secrets.reference_store",),
        ),
        SOTService(
            name="secrets.access_credential_format",
            module="app.services.access_credential_secret",
            owns=(
                "access credential representation classification",
                "one-way RADIUS hash preservation policy",
                "explicit cleartext marker normalization",
            ),
        ),
        SOTService(
            name="secrets.credential_integrity",
            module="app.services.credential_key_rotation",
            owns=(
                "credential integrity classification",
                "plaintext credential remediation",
                "credential integrity observability projection",
                "credential re-encryption convergence",
            ),
            depends_on=(
                "secrets.access_credential_format",
                "secrets.credential_crypto",
                "observability.recording",
                "runtime.db_sessions",
            ),
        ),
        SOTService(
            name="secrets.rotation",
            module="app.services.credential_rotation_schedule",
            owns=(
                "scheduled credential key lifecycle",
                "rotation grace period",
            ),
            depends_on=(
                "secrets.reference_store",
                "secrets.credential_integrity",
                "runtime.db_sessions",
            ),
        ),
        SOTService(
            name="secrets.credential_recovery",
            module="app.services.credential_lifecycle_cleanup",
            owns=(
                "lost-key credential recovery planning",
                "lifecycle-safe unrecoverable credential cleanup",
                "reviewed cleanup plan digest enforcement",
            ),
            depends_on=(
                "secrets.credential_integrity",
                "network.identity",
                "network.radius_sessions",
                "access.radius_state",
                "runtime.db_sessions",
                "observability.recording",
            ),
        ),
        SOTService(
            name="secrets.settings_migration",
            module="app.services.settings_secret_cleanup",
            owns=(
                "noncanonical secret-setting discovery",
                "OpenBao secret-setting migration",
                "secret-setting reference replacement",
            ),
            depends_on=(
                "secrets.reference_store",
                "secrets.settings_policy",
                "secrets.credential_crypto",
            ),
        ),
    ),
    entrypoints=(
        "app.tasks.security",
        "app.web.admin.system",
        "scripts.one_off.migrate_secret_settings_to_openbao",
        "app.services.*",
    ),
    rule="Bootstrap secrets use environment or mounted files; application "
    "secrets use references; high-cardinality credentials use the "
    "declared encrypted-field inventory. Callers never choose storage.",
)
