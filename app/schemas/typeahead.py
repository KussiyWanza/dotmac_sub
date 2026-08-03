from uuid import UUID

from pydantic import BaseModel


class TypeaheadItem(BaseModel):
    id: UUID
    label: str
    ref: str | None = None
    type: str | None = None
    email: str | None = None
    phone: str | None = None
    organization: str | None = None
    address: str | None = None
    subscriber_number: str | None = None
    account_number: str | None = None
    account_status: str | None = None
    plan: str | None = None
    service_address: str | None = None
    reseller_controlled: bool | None = None
