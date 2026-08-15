from uuid import uuid4

from app.services.staff_party_authentication import (
    StaffProjectionError,
    StaffProjectionRefusal,
)


def test_staff_projection_error_allows_runtime_traceback_state() -> None:
    """Domain errors must remain usable by context managers and web adapters."""

    credential_id = uuid4()
    error = StaffProjectionError(
        StaffProjectionRefusal.projection_missing,
        credential_id,
    )

    error.__traceback__ = None

    assert error.refusal is StaffProjectionRefusal.projection_missing
    assert error.credential_id == credential_id
    assert str(error) == f"staff_projection_missing for credential {credential_id}"
