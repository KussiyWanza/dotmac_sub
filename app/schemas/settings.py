from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType


def _stores_as_json(value_type: SettingValueType) -> bool:
    """Does this value type live in ``value_json``?

    Asked of the ONE authority rather than restated here. This validator used
    to test ``value_type == SettingValueType.json`` literally, which is the same
    closed-list defect migration 512 removed from the CHECK constraint: a second
    JSON-stored type (``list``, ``money``) was rejected as "non-json settings
    require value_text", so a setting whose value is an array could not be
    written through the API at all.

    Which column a type uses is a property of the TYPE —
    ``ValueTypeSpec.storage`` in ``dotmac_kernel.setting_value_types`` — and
    nothing outside that registry is allowed to know it.

    An UNDECLARED type is treated as text-stored rather than raising: this is a
    schema-layer validator on the read and write path, and the loud rejection
    belongs at the model write boundary (``_reject_undeclared_value_type``),
    which sees every writer rather than only this one.
    """

    from dotmac_kernel.setting_value_types import active_setting_value_types

    registry = active_setting_value_types()
    if not registry.is_declared(str(value_type)):
        return False
    return registry.require(str(value_type)).storage == "json"


class DomainSettingBase(BaseModel):
    domain: SettingDomain
    key: str
    value_type: SettingValueType = SettingValueType.string
    value_text: str | None = None
    value_json: dict | list | bool | int | str | None = None
    is_secret: bool = False
    is_active: bool = True


class DomainSettingCreate(DomainSettingBase):
    @model_validator(mode="after")
    def _validate_value_alignment(self) -> "DomainSettingCreate":
        # Same closed-list defect as `DomainSettingUpdate` below had: `list`
        # and `money` are JSON-stored too, and the type is what knows.
        if self.value_type is not None and _stores_as_json(self.value_type):
            if self.value_json is None or self.value_text is not None:
                raise ValueError(f"{self.value_type} settings require value_json only.")
        else:
            if self.value_text is None:
                raise ValueError("non-json settings require value_text.")
            if isinstance(self.value_json, (dict, list)):
                raise ValueError("non-json settings require primitive value_json.")
        return self


class DomainSettingUpdate(BaseModel):
    domain: SettingDomain | None = None
    key: str | None = None
    value_type: SettingValueType | None = None
    value_text: str | None = None
    value_json: dict | list | bool | int | str | None = None
    is_secret: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_value_alignment(self) -> "DomainSettingUpdate":
        fields_set = self.model_fields_set
        if {"value_type", "value_text", "value_json"} & fields_set:
            if self.value_type is not None and _stores_as_json(self.value_type):
                if self.value_json is None or self.value_text is not None:
                    raise ValueError(
                        f"{self.value_type} settings require value_json only."
                    )
            elif self.value_type is not None:
                if self.value_text is None:
                    raise ValueError("non-json settings require value_text.")
                if isinstance(self.value_json, (dict, list)):
                    raise ValueError("non-json settings require primitive value_json.")
            else:
                if self.value_text is not None and self.value_json is not None:
                    raise ValueError("Provide only one of value_text or value_json.")
                if isinstance(self.value_json, (dict, list)):
                    raise ValueError("value_type is required when setting value_json.")
        return self


class DomainSettingRead(DomainSettingBase):
    id: str | UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
