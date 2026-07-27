"""Resolve the temporary live-chat authority from the canonical control plane."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value

SETTING_KEY = "chat_session_authority"


class ChatSessionAuthority(StrEnum):
    SELFCARE = "selfcare"
    CRM = "crm"


@dataclass(frozen=True)
class ChatSessionAuthorityDecision:
    authority: ChatSessionAuthority
    source: str = "control.settings_spec"


def resolve_chat_session_authority(db: Session) -> ChatSessionAuthorityDecision:
    raw = str(resolve_value(db, SettingDomain.comms, SETTING_KEY) or "").strip()
    try:
        authority = ChatSessionAuthority(raw)
    except ValueError:
        authority = ChatSessionAuthority.SELFCARE
    return ChatSessionAuthorityDecision(authority=authority)
