"""PostgreSQL constraint evidence for assignment-scoped configuration heads."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.network import OntAssignment, OntUnit
from app.models.ont_service_configuration import OntServiceConfigurationHead

pytestmark = pytest.mark.integration


def test_concurrent_head_creation_yields_one_assignment_head(engine):
    """The migrated unique constraint closes the create-on-miss race."""

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as setup:
        ont = OntUnit(
            serial_number=f"CONFIG-CONC-{uuid.uuid4().hex[:12]}",
            is_active=True,
        )
        setup.add(ont)
        setup.flush()
        assignment = OntAssignment(ont_unit_id=ont.id, active=True)
        setup.add(assignment)
        setup.commit()
        ont_id = ont.id
        assignment_id = assignment.id

    barrier = threading.Barrier(2)

    def create_head() -> str:
        with factory() as session:
            barrier.wait(timeout=10)
            session.add(
                OntServiceConfigurationHead(
                    ont_unit_id=ont_id,
                    assignment_id=assignment_id,
                    current_revision=0,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return "conflict"
            return "created"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = (pool.submit(create_head), pool.submit(create_head))
            results = sorted(future.result(timeout=15) for future in outcomes)
        assert results == ["conflict", "created"]
        with factory() as verify:
            heads = list(
                verify.scalars(
                    select(OntServiceConfigurationHead).where(
                        OntServiceConfigurationHead.assignment_id == assignment_id
                    )
                )
            )
            assert len(heads) == 1
            assert heads[0].current_revision == 0
    finally:
        with factory() as cleanup:
            cleanup.execute(
                delete(OntServiceConfigurationHead).where(
                    OntServiceConfigurationHead.assignment_id == assignment_id
                )
            )
            cleanup.execute(
                delete(OntAssignment).where(OntAssignment.id == assignment_id)
            )
            cleanup.execute(delete(OntUnit).where(OntUnit.id == ont_id))
            cleanup.commit()
