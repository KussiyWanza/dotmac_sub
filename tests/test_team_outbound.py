from __future__ import annotations

from app.models.service_team import ServiceTeam
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import email as email_service
from app.services import team_outbound
from app.services.domain_settings import notification_settings


def _smtp_sender(db_session, key: str, *, host: str) -> None:
    email_service.upsert_smtp_sender(
        db_session,
        sender_key=key,
        host=host,
        port=587,
        username=f"{key}-user",
        password=f"{key}-pass",
        from_email=f"{key}@example.com",
        from_name=key.title(),
        use_tls=True,
        use_ssl=False,
        is_active=True,
    )


def _activity_sender(db_session, activity: str, sender_key: str) -> None:
    notification_settings.upsert_by_key(
        db_session,
        f"smtp_activity_sender.{activity}",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=sender_key,
        ),
    )


def _team(db_session, name: str = "Outbound Team") -> ServiceTeam:
    team = ServiceTeam(name=name, is_active=True)
    db_session.add(team)
    db_session.commit()
    return team


def test_caller_declared_activity_resolves_activity_sender(db_session):
    _smtp_sender(db_session, "support", host="smtp.support.local")
    _activity_sender(db_session, "support_ticket", "support")
    team = _team(db_session)

    resolved = team_outbound.resolve_team_email_sender(
        db_session, team=team, activity="support_ticket"
    )

    assert resolved.activity == "support_ticket"
    assert resolved.config["sender_key"] == "support"


def test_without_declared_activity_delivery_uses_notification_default(db_session):
    # Team identity (including capabilities) never derives delivery behavior:
    # with no caller-declared activity and no operator metadata, the resolution
    # carries no activity and the notification layer applies its default.
    team = _team(db_session)

    unresolved = team_outbound.resolve_team_email_sender(db_session, team=team)

    assert unresolved.activity is None


def test_team_metadata_activity_overrides_caller_activity(db_session):
    _smtp_sender(db_session, "field", host="smtp.field.local")
    _activity_sender(db_session, "field_service", "field")
    team = _team(db_session, name="Field Ops")
    team.metadata_ = {
        team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY: "field_service"
    }
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(
        db_session, team=team, activity="support_ticket"
    )

    assert resolved.activity == "field_service"
    assert resolved.config["sender_key"] == "field"


def test_route_sender_metadata_overrides_team_metadata(db_session):
    _smtp_sender(db_session, "team_support", host="smtp.team.local")
    _smtp_sender(db_session, "route_support", host="smtp.route.local")
    team = _team(db_session, name="Support Routes")
    team.metadata_ = {"outbound_email_sender_key": "team_support"}
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(
        db_session,
        team=team,
        metadata_override={
            team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "route_support",
            team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY: "support_ticket",
        },
    )

    assert resolved.sender_key == "route_support"
    assert resolved.activity == "support_ticket"
    assert resolved.config["host"] == "smtp.route.local"
