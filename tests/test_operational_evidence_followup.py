from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.poller.mikrotik_poller import DevicePool, MikroTikConnection
from app.services import db_error_observability, operational_checks
from app.services.db_error_observability import statement_fingerprint
from app.services.payment_reconciliation import TopupReconciliationBacklog
from app.services.web_network_ont_actions import device_actions


def test_running_config_releases_read_transaction_before_ssh(monkeypatch):
    transaction_open = True
    olt_id = uuid4()
    ont_id = uuid4()
    olt = SimpleNamespace(
        id=olt_id,
        name="Garki OLT",
        hostname="garki-olt",
        mgmt_ip="192.0.2.10",
        vendor="Huawei",
        model="MA5800",
        firmware_version="V1",
        software_version="V1",
        ssh_username="operator",
        ssh_password="encrypted",
        ssh_port=22,
        rate_limit_ops_per_minute=10,
    )
    ont = SimpleNamespace(
        id=ont_id,
        serial_number="HWTC12345678",
        external_id="1",
        olt_device=olt,
    )
    pon = SimpleNamespace(name="0/1/0", olt=olt)
    assignment = SimpleNamespace(pon_port=pon)

    class _Db:
        def get(self, _model, _id):
            return ont

    def finish_read(_db):
        nonlocal transaction_open
        transaction_open = False

    def run_cli(_target, _command):
        assert transaction_open is False
        return True, "ok", "output"

    monkeypatch.setattr(device_actions, "finish_read_transaction", finish_read)
    monkeypatch.setattr(
        "app.services.web_network_ont_assignments.active_assignment_for_ont_id",
        lambda *_args: assignment,
    )
    monkeypatch.setattr("app.services.network.olt_ssh.run_cli_command", run_cli)
    monkeypatch.setattr(
        "app.services.network.huawei_command_profiles.get_huawei_command_profile",
        lambda _olt: SimpleNamespace(
            display_ont_info=lambda fsp, onu_id: f"display ont info {fsp} {onu_id}"
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_read_cache.olt_cache.get",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_read_cache.olt_cache.set",
        lambda *_args, **_kwargs: None,
    )

    result = device_actions.fetch_olt_running_config(_Db(), str(ont_id))

    assert result.error is None
    assert result.ont is not ont
    assert result.olt is not olt
    assert result.ont.serial_number == "HWTC12345678"


def test_poller_failure_snapshot_explains_attempt_impact_and_retry():
    now = datetime.now(UTC)
    device_id = uuid4()
    subscription_id = uuid4()
    connection = MikroTikConnection(
        device_id=device_id,
        display_name="Garki Core",
        host="192.0.2.20",
        username="operator",
        password="test-ciphertext",
    )
    connection._consecutive_failures = 9
    connection._last_attempt = now
    connection._last_successful_poll = now - timedelta(hours=1)
    connection._last_error_category = "no_route_to_host"
    connection._last_error = "No route to host"
    pool = DevicePool()
    pool._connections[device_id] = connection
    pool._queue_mappings[device_id] = {"customer": subscription_id}

    snapshot = pool.health_snapshot()

    assert snapshot["devices_failing"] == 1
    row = snapshot["device_failures"][0]
    assert row["name"] == "Garki Core"
    assert row["error_category"] == "no_route_to_host"
    assert row["services_without_live_bandwidth"] == 1
    assert row["next_attempt_at"] is not None


def test_database_statement_correlation_is_stable_and_redacted():
    first = statement_fingerprint(
        "SELECT subscribers.id FROM subscribers WHERE subscribers.email = %(email)s"
    )
    second = statement_fingerprint(
        " select  subscribers.id from subscribers where subscribers.email = %(email)s "
    )

    assert first == second
    assert first is not None
    assert "email" not in first


def test_idle_transaction_pool_invalidation_records_transaction_owner(
    monkeypatch,
    caplog,
):
    owner = db_error_observability.DatabaseTransactionOwner(
        started_at=10.0,
        request_id=None,
        task_name="app.tasks.billing.reconcile",
        task_id="task-123",
    )
    connection_record = SimpleNamespace(
        info={db_error_observability._CONNECTION_OWNER_KEY: owner}
    )
    monkeypatch.setattr(db_error_observability, "monotonic", lambda: 75.5)

    with caplog.at_level("ERROR"):
        db_error_observability._handle_pool_invalidate(
            None,
            connection_record,
            RuntimeError("terminating connection due to idle-in-transaction timeout"),
        )

    record = next(
        item
        for item in caplog.records
        if item.message == "database_pool_connection_invalidated"
    )
    assert record.category == "idle_transaction_timeout"
    assert record.task_name == "app.tasks.billing.reconcile"
    assert record.task_id == "task-123"
    assert record.transaction_age_seconds == 65.5


def _topup_reconciliation_backlog(
    observed_at: datetime,
    *,
    pending_fresh: int = 0,
    pending_due: int = 0,
    pending_cooling_down: int = 0,
    pending_outside_window: int = 0,
    terminal_due: int = 0,
    terminal_cooling_down: int = 0,
    terminal_outside_window: int = 0,
    oldest_pending_at: datetime | None = None,
    oldest_pending_due_at: datetime | None = None,
    oldest_terminal_due_at: datetime | None = None,
) -> TopupReconciliationBacklog:
    return TopupReconciliationBacklog(
        pending_total=(
            pending_fresh + pending_due + pending_cooling_down + pending_outside_window
        ),
        pending_fresh=pending_fresh,
        pending_due=pending_due,
        pending_cooling_down=pending_cooling_down,
        pending_outside_window=pending_outside_window,
        terminal_recovery_total=(
            terminal_due + terminal_cooling_down + terminal_outside_window
        ),
        terminal_recovery_due=terminal_due,
        terminal_recovery_cooling_down=terminal_cooling_down,
        terminal_recovery_outside_window=terminal_outside_window,
        oldest_pending_at=oldest_pending_at,
        oldest_pending_due_created_at=oldest_pending_due_at,
        oldest_terminal_due_created_at=oldest_terminal_due_at,
        stale_before=observed_at - timedelta(minutes=15),
        oldest_eligible_at=observed_at - timedelta(days=7),
    )


def _stub_paystack_check(
    monkeypatch,
    *,
    now: datetime,
    result: dict[str, object],
    backlog: TopupReconciliationBacklog,
    webhook_at: datetime | None,
) -> None:
    monkeypatch.setattr(
        operational_checks,
        "_paystack_binding_evidence",
        lambda _db: ("Capabilities are enabled.", True, True),
    )
    monkeypatch.setattr(
        operational_checks,
        "_task_row",
        lambda _db, _task_name: SimpleNamespace(
            enabled=True,
            interval_seconds=1800,
        ),
    )
    monkeypatch.setattr(
        operational_checks,
        "_task_result",
        lambda _task_name: (result, now - timedelta(minutes=5)),
    )
    monkeypatch.setattr(
        operational_checks.job_heartbeat,
        "get_last_success",
        lambda _task_name: now - timedelta(minutes=5),
    )

    def read_backlog(_db, *, observed_at, provider_types):
        assert observed_at == now
        assert provider_types == (operational_checks.PaymentProviderType.paystack,)
        return backlog

    monkeypatch.setattr(
        operational_checks,
        "topup_reconciliation_backlog",
        read_backlog,
    )
    monkeypatch.setattr(
        operational_checks,
        "_latest_paystack_webhook_at",
        lambda _db: webhook_at,
    )


def test_paystack_operational_check_exposes_webhook_and_reconciliation_gap(
    db_session, monkeypatch
):
    now = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
    _stub_paystack_check(
        monkeypatch,
        now=now,
        result={
            "status": "partial",
            "detail": {
                "selected": 8,
                "checked": 8,
                "unchanged": 5,
                "errors": 3,
                "pending_due_remaining": 3,
                "terminal_due_remaining": 2,
                "outside_window": 0,
                "saturated": True,
                "partial": True,
            },
        },
        backlog=_topup_reconciliation_backlog(
            now,
            pending_fresh=1,
            pending_due=3,
            terminal_due=2,
            terminal_cooling_down=2,
            oldest_pending_at=now - timedelta(hours=1),
            oldest_pending_due_at=now - timedelta(hours=1),
            oldest_terminal_due_at=now - timedelta(days=2),
        ),
        webhook_at=None,
    )

    check = operational_checks.paystack_payment_check(db_session, now=now)

    assert check.needs_attention is True
    assert check.last_result == (
        "Reconciliation selected 8, checked 8, left 5 unchanged, and encountered "
        "3 error(s); the last run left 3 pending and 2 terminal attempt(s) due, "
        "with 0 outside the automatic window."
    )
    assert "pending due=3 (oldest age 1.0 hours)" in check.evidence
    assert "terminal due=2 (oldest age 2.0 days)" in check.evidence
    assert operational_checks.PAYSTACK_WEBHOOK_PATH in check.expected
    assert "Set the Paystack live webhook URL" in check.next_step


def test_paystack_due_pending_needs_attention_even_after_a_verified_webhook(
    db_session, monkeypatch
):
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    _stub_paystack_check(
        monkeypatch,
        now=now,
        result={
            "status": "ok",
            "detail": {
                "selected": 1,
                "checked": 1,
                "unchanged": 1,
                "errors": 0,
                "pending_due_remaining": 0,
                "terminal_due_remaining": 0,
                "outside_window": 0,
                "saturated": False,
                "partial": False,
            },
        },
        backlog=_topup_reconciliation_backlog(
            now,
            pending_due=2,
            terminal_cooling_down=1,
            oldest_pending_at=now - timedelta(hours=3),
            oldest_pending_due_at=now - timedelta(hours=3),
        ),
        webhook_at=now - timedelta(minutes=10),
    )

    check = operational_checks.paystack_payment_check(db_session, now=now)

    assert check.needs_attention is True
    assert "pending due=2 (oldest age 3.0 hours)" in check.evidence
    assert "Inspect the due pending Paystack intents" in check.next_step


def test_paystack_partial_last_result_needs_attention_with_empty_live_backlog(
    db_session, monkeypatch
):
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    _stub_paystack_check(
        monkeypatch,
        now=now,
        result={
            "status": "partial",
            "detail": {
                "selected": 1,
                "checked": 1,
                "unchanged": 0,
                "errors": 1,
                "pending_due_remaining": 0,
                "terminal_due_remaining": 0,
                "outside_window": 0,
                "saturated": False,
                "partial": True,
            },
        },
        backlog=_topup_reconciliation_backlog(now),
        webhook_at=now - timedelta(minutes=10),
    )

    check = operational_checks.paystack_payment_check(db_session, now=now)

    assert check.needs_attention is True
    assert "encountered 1 error(s)" in check.last_result
    assert "Inspect the partial reconciliation result" in check.next_step


def test_paystack_failed_last_result_needs_attention_with_fresh_success_heartbeat(
    db_session, monkeypatch
):
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    _stub_paystack_check(
        monkeypatch,
        now=now,
        result={"status": "error", "detail": {"error": "provider timeout"}},
        backlog=_topup_reconciliation_backlog(now),
        webhook_at=now - timedelta(minutes=10),
    )

    check = operational_checks.paystack_payment_check(db_session, now=now)

    assert check.needs_attention is True
    assert check.last_result == (
        "The last reconciliation execution failed; result counters are not "
        "treated as successful evidence."
    )
    assert "Inspect the failed reconciliation task" in check.next_step


def test_paystack_outside_window_needs_explicit_finance_reconciliation(
    db_session, monkeypatch
):
    now = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    _stub_paystack_check(
        monkeypatch,
        now=now,
        result={"status": "ok", "detail": {}},
        backlog=_topup_reconciliation_backlog(
            now,
            pending_outside_window=1,
            terminal_outside_window=1,
            oldest_pending_at=now - timedelta(days=8),
        ),
        webhook_at=now - timedelta(minutes=10),
    )

    check = operational_checks.paystack_payment_check(db_session, now=now)

    assert check.needs_attention is True
    assert "outside-window=1" in check.evidence
    assert "canonical finance owner" in check.next_step
