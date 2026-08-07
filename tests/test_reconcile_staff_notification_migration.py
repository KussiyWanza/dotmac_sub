from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "498_reconcile_staff_notification_inbox.py"
)


def test_reconciliation_is_idempotent_and_staff_scoped():
    source = MIGRATION.read_text(encoding="utf-8")

    assert "ON CONFLICT (source_notification_id) DO NOTHING" in source
    assert "JOIN system_users AS su" in source
    assert "lower(su.email) = lower(n.recipient)" in source
    assert "WHERE n.channel = 'push'" in source
    assert "AND n.is_active" in source


def test_reconciliation_does_not_create_false_unread_notifications():
    source = MIGRATION.read_text(encoding="utf-8")

    assert "COALESCE(n.sent_at, n.created_at, now()) AS historical_read_at" in source
    assert "eligible.historical_read_at" in source
