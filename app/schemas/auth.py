from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from app.models.auth import AuthProvider, MFAMethodType, SessionStatus


class UserCredentialBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    provider: AuthProvider = AuthProvider.local
    username: str | None = Field(default=None, max_length=150)
    radius_server_id: UUID | None = None
    must_change_password: bool = False
    password_updated_at: datetime | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    is_active: bool = True


class UserCredentialCreate(UserCredentialBase):
    password_hash: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _validate_local_credentials(self) -> UserCredentialCreate:
        if self.provider == AuthProvider.local:
            if not self.username or not self.password_hash:
                raise ValueError(
                    "Local credentials require username and password_hash."
                )
        return self


class UserCredentialUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    provider: AuthProvider | None = None
    username: str | None = Field(default=None, max_length=150)
    password_hash: str | None = Field(default=None, max_length=255)
    radius_server_id: UUID | None = None
    must_change_password: bool | None = None
    password_updated_at: datetime | None = None
    failed_login_attempts: int | None = None
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_local_credentials(self) -> UserCredentialUpdate:
        fields_set = self.model_fields_set
        if "provider" in fields_set and self.provider == AuthProvider.local:
            if "username" not in fields_set or not self.username:
                raise ValueError("Local credentials require username.")
            if "password_hash" not in fields_set or not self.password_hash:
                raise ValueError("Local credentials require password_hash.")
        if "username" in fields_set and not self.username:
            if self.provider == AuthProvider.local or "provider" not in fields_set:
                raise ValueError("Cannot clear username for local credentials.")
        if "password_hash" in fields_set and not self.password_hash:
            if self.provider == AuthProvider.local or "provider" not in fields_set:
                raise ValueError("Cannot clear password_hash for local credentials.")
        return self


class UserCredentialRead(UserCredentialBase):
    model_config = ConfigDict(from_attributes=True)

    # Staff-owned rows bind to system_user_id (or reseller_user_id), so
    # subscriber_id is legitimately NULL on them — the model allows exactly
    # one principal. Reads must not require it; writes stay strict.
    subscriber_id: UUID | None = None

    id: UUID
    created_at: datetime
    updated_at: datetime


class MFAMethodBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    method_type: MFAMethodType
    label: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    is_primary: bool = False
    enabled: bool = True
    is_active: bool = True
    verified_at: datetime | None = None
    last_used_at: datetime | None = None


class MFAMethodCreate(MFAMethodBase):
    secret: str | None = Field(default=None, max_length=255)


class MFAMethodUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    method_type: MFAMethodType | None = None
    label: str | None = Field(default=None, max_length=120)
    secret: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    is_primary: bool | None = None
    enabled: bool | None = None
    is_active: bool | None = None
    verified_at: datetime | None = None
    last_used_at: datetime | None = None


class MFAMethodRead(MFAMethodBase):
    model_config = ConfigDict(from_attributes=True)

    # Staff-owned rows bind to system_user_id (or reseller_user_id), so
    # subscriber_id is legitimately NULL on them — the model allows exactly
    # one principal. Reads must not require it; writes stay strict.
    subscriber_id: UUID | None = None

    id: UUID
    created_at: datetime
    updated_at: datetime


class SessionBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    status: SessionStatus = SessionStatus.active
    token_hash: str = Field(min_length=1, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    last_seen_at: datetime | None = None
    expires_at: datetime
    revoked_at: datetime | None = None


class SessionCreate(SessionBase):
    pass


class SessionUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    status: SessionStatus | None = None
    token_hash: str | None = Field(default=None, min_length=1, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    last_seen_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class SessionRead(SessionBase):
    model_config = ConfigDict(from_attributes=True)

    # Staff-owned rows bind to system_user_id (or reseller_user_id), so
    # subscriber_id is legitimately NULL on them — the model allows exactly
    # one principal. Reads must not require it; writes stay strict.
    subscriber_id: UUID | None = None

    id: UUID
    created_at: datetime


class ApiKeyBase(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    label: str | None = Field(default=None, max_length=120)
    key_hash: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=list)
    is_active: bool = True
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreate(ApiKeyBase):
    pass


class ApiKeyGenerateRequest(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    label: str | None = Field(default=None, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class ApiKeyUpdate(BaseModel):
    subscriber_id: UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    key_hash: str | None = Field(default=None, min_length=1, max_length=255)
    scopes: list[str] | None = None
    is_active: bool | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyRead(ApiKeyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime

    @field_serializer("key_hash")
    def _mask_key_hash(self, value: str) -> str:
        if not value:
            return ""
        suffix = value[-4:]
        return f"{'*' * max(len(value) - 4, 4)}{suffix}"


class ApiKeyGenerateResponse(BaseModel):
    key: str
    api_key: ApiKeyRead
