"""Preview or apply the explicit CRM ticket-observation capability cutover.

Preview is the default and is read-only. Apply requires the exact reviewed
installation, job, and preview fingerprint. It invokes the installation and
job owners separately; scheduler readiness remains fail-closed between those
idempotent steps.
"""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from app.models.integration import IntegrationTargetType
from app.models.integration_platform import IntegrationBindingState
from app.services import integration as integration_jobs
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import installations
from app.services.integrations.connectors.dotmac_crm import (
    CRM_TICKET_OBSERVATION_CAPABILITY,
)
from app.services.integrations.crm_ticket_readiness import (
    preview_crm_ticket_cutover,
)
from app.services.owner_commands import CommandContext


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--installation-id", type=_uuid)
    parser.add_argument("--job-id", type=_uuid)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.apply:
        missing = [
            name
            for name, value in (
                ("--installation-id", args.installation_id),
                ("--job-id", args.job_id),
                ("--expected-fingerprint", args.expected_fingerprint),
                ("--idempotency-key", args.idempotency_key),
                ("--actor", args.actor),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
    elif any(
        value
        for value in (
            args.expected_fingerprint,
            args.idempotency_key,
            args.actor,
            args.reason,
        )
    ):
        parser.error("apply-only arguments require --apply")
    return args


def _preview(
    *,
    installation_id: UUID | None,
    job_id: UUID | None,
):
    with db_session_adapter.read_session() as db:
        return preview_crm_ticket_cutover(
            db,
            installation_id=installation_id,
            job_id=job_id,
        )


def main() -> int:
    args = parse_args()
    reviewed = _preview(
        installation_id=args.installation_id,
        job_id=args.job_id,
    )
    if not args.apply:
        print(json.dumps(reviewed.to_dict(), indent=2, sort_keys=True))
        return 0 if reviewed.eligible else 2
    if reviewed.fingerprint != args.expected_fingerprint:
        raise SystemExit(
            "CRM ticket cutover state changed after review; run preview again."
        )
    if not reviewed.eligible:
        print(json.dumps(reviewed.to_dict(), indent=2, sort_keys=True))
        return 2
    if (
        reviewed.installation_id is None
        or reviewed.job_id is None
        or reviewed.connector_version is None
        or reviewed.manifest_digest is None
        or reviewed.job_is_active is None
    ):
        raise SystemExit("CRM ticket cutover preview lacks exact target state.")

    correlation_context = CommandContext.system(
        actor=args.actor,
        scope=installations.CAPABILITY_PROVISIONING_SCOPE,
        reason=args.reason,
        idempotency_key=f"{args.idempotency_key}:binding",
    )
    with db_session_adapter.owner_command_session() as db:
        binding_result = installations.provision_installation_capability(
            db,
            installations.ProvisionCapabilityCommand(
                installation_id=reviewed.installation_id,
                capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
                expected_installed_pin=installations.ManifestPin(
                    connector_version=reviewed.connector_version,
                    manifest_digest=reviewed.manifest_digest,
                ),
                expected_binding_id=reviewed.binding_id,
                expected_binding_state=(
                    IntegrationBindingState(reviewed.binding_state)
                    if reviewed.binding_state is not None
                    else None
                ),
                capability_scope={},
                policy={"default": True},
            ),
            context=correlation_context,
        )

    with db_session_adapter.owner_command_session() as db:
        job_result = integration_jobs.activate_capability_job(
            db,
            integration_jobs.ActivateCapabilityJobCommand(
                job_id=reviewed.job_id,
                capability_binding_id=binding_result.capability_binding_id,
                capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
                expected_target_type=IntegrationTargetType.crm,
                expected_existing_binding_id=reviewed.job_binding_id,
                expected_is_active=reviewed.job_is_active,
            ),
            context=CommandContext.system(
                actor=args.actor,
                scope=integration_jobs.CAPABILITY_JOB_ACTIVATION_SCOPE,
                reason=args.reason,
                correlation_id=correlation_context.correlation_id,
                causation_id=correlation_context.command_id,
                idempotency_key=f"{args.idempotency_key}:job",
            ),
        )

    final = _preview(
        installation_id=reviewed.installation_id,
        job_id=reviewed.job_id,
    )
    payload = {
        "binding_replayed": binding_result.replayed,
        "final": final.to_dict(),
        "job_replayed": job_result.replayed,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if final.already_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
