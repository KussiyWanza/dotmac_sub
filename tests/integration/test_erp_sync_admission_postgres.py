"""Admission concurrency acceptance on the migration-built PostgreSQL lane."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.erp_domain_sync import ErpOperationalSyncState
from app.services.dotmac_erp.domain_sync import sync_operational_domains
from app.services.dotmac_erp.operational_contracts import OperationalSyncExecution
from app.services.owner_commands import CommandContext


def test_singleton_is_seeded_by_migration_and_overlapping_run_is_refused(engine):
    with Session(engine) as holder, Session(engine) as contender:
        state = holder.scalar(
            select(ErpOperationalSyncState)
            .where(ErpOperationalSyncState.id == 1)
            .with_for_update()
        )
        assert state is not None, "real migration must seed admission singleton"
        outcome = sync_operational_domains(
            contender,
            command=OperationalSyncExecution(),
            context=CommandContext.system(
                actor="test-suite",
                scope="erp-operational-context",
                reason="verify overlap refusal",
            ),
        )
        assert outcome.status == "already_running"
        assert outcome.skipped == "already_running"
        holder.rollback()
