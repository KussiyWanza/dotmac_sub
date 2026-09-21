"""Field-location ingest commits through one registered owner command.

record_ping and record_batch used to call db.commit() directly, with no
per-row isolation. A future duplicate-ping identity constraint (a separate,
later change) would then surface an IntegrityError at db.flush() with no
savepoint around it, turning one duplicate row into a whole-batch 500. This
guard keeps the owner-command boundary and per-row savepoint in place so
that regression cannot silently return.
"""

from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_field_location_ingest_is_a_registered_owner_command() -> None:
    owner = service_relationship("operations.field_location_ingest")
    assert owner.module == "app.services.field.location_tracking"
    assert owner.contract is not None
    assert owner.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert {concern.name for concern in owner.contract.concerns} == {
        "field-location ping ingest",
        "field-location sharing preference",
    }


def test_location_tracking_has_no_bare_commit() -> None:
    source = (ROOT / "app/services/field/location_tracking.py").read_text(
        encoding="utf-8"
    )
    assert "db.commit(" not in source
    # Each write path enters the owner-command boundary exactly once, and a
    # batch additionally isolates each row in its own savepoint.
    assert source.count("execute_owner_command(") == 3
    assert "execute_owner_savepoint(" in source


def test_field_location_ingest_route_never_writes_directly() -> None:
    source = (ROOT / "app/api/field/locations.py").read_text(encoding="utf-8")
    for forbidden in ("db.query(", "db.get(", "db.commit(", ".session()"):
        assert forbidden not in source
    # Both mutating routes release any implicit read transaction opened by
    # the auth dependency before entering the owner-command boundary.
    assert source.count("release_read_transaction(db)") == 2
