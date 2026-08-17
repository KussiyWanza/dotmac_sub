"""Typed admin-system-user form adapter for the staff identity owner."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.services import staff_provisioning
from app.services.owner_commands import CommandContext


@dataclass(frozen=True)
class StaffEditForm:
    first_name: str
    last_name: str
    display_name: str | None
    email: str
    phone: str | None
    field_technician_access: bool
    new_password: str | None
    confirm_password: str | None
    require_password_change: bool


def parse_edit_form(form_data) -> StaffEditForm:
    return StaffEditForm(
        first_name=str(form_data.get("first_name", "")),
        last_name=str(form_data.get("last_name", "")),
        display_name=(str(form_data.get("display_name") or "").strip() or None),
        email=str(form_data.get("email", "")),
        phone=(str(form_data.get("phone") or "").strip() or None),
        field_technician_access=str(
            form_data.get("field_technician_access") or ""
        ).lower()
        in {"1", "true", "yes", "on"},
        new_password=(str(form_data.get("new_password") or "") or None),
        confirm_password=(str(form_data.get("confirm_password") or "") or None),
        require_password_change=str(
            form_data.get("require_password_change") or ""
        ).lower()
        in {"1", "true", "yes", "on"},
    )


def build_update_command(
    *,
    user_id: UUID,
    context: CommandContext,
    form: StaffEditForm,
    can_update_password: bool,
) -> staff_provisioning.UpdateStaffIdentityCommand:
    if form.new_password or form.confirm_password:
        if not can_update_password:
            raise ValueError("Only admins can update passwords.")
        if not form.new_password or not form.confirm_password:
            raise ValueError("Password and confirmation are required.")
        if form.new_password != form.confirm_password:
            raise ValueError("Passwords do not match.")
    return staff_provisioning.UpdateStaffIdentityCommand(
        context=context,
        user_id=user_id,
        fields=frozenset(staff_provisioning.StaffIdentityField),
        first_name=form.first_name,
        last_name=form.last_name,
        display_name=form.display_name,
        email=form.email,
        phone=form.phone,
        new_password=form.new_password,
        require_password_change=form.require_password_change,
        field_technician_access=form.field_technician_access,
    )
