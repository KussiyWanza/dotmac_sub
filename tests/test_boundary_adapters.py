from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4


def test_queue_adapter_delegates_to_enqueue_function() -> None:
    from app.services.queue_adapter import CeleryQueueAdapter, QueueMessage

    calls = []

    def fake_enqueue(task_name, **kwargs):
        calls.append((task_name, kwargs))
        return SimpleNamespace(id="task-123")

    adapter = CeleryQueueAdapter(enqueue_func=fake_enqueue)
    result = adapter.enqueue(
        QueueMessage(
            task_name="app.tasks.billing.run",
            args=("invoice-1",),
            kwargs={"force": True},
            queue="billing",
            correlation_id="corr-1",
            source="unit-test",
        )
    )

    assert result.queued is True
    assert result.task_id == "task-123"
    assert result.queue == "billing"
    assert calls[0][0] == "app.tasks.billing.run"
    assert calls[0][1]["queue"] == "billing"
    assert calls[0][1]["correlation_id"] == "corr-1"


def test_queue_adapter_returns_failure_when_backend_unavailable() -> None:
    from app.services.queue_adapter import CeleryQueueAdapter, QueueMessage

    def unavailable(_task_name, **_kwargs):
        raise RuntimeError("broker unavailable")

    adapter = CeleryQueueAdapter(enqueue_func=unavailable)
    result = adapter.enqueue(
        QueueMessage(
            task_name="app.tasks.billing.run",
            queue="billing",
        )
    )

    assert result.queued is False
    assert result.task_name == "app.tasks.billing.run"
    assert result.queue == "billing"
    assert result.error == "broker unavailable"


def test_adapter_result_base_supports_domain_results() -> None:
    from app.services.adapters.base import AdapterResult, AdapterStatus
    from app.services.network.olt_protocol_adapters import OltOperationResult

    result = OltOperationResult(
        success=True,
        message="created",
        data={"service_port": 401},
        ont_id=7,
    )
    failure = AdapterResult.from_exception(
        RuntimeError("boom"),
        operation="unit test adapter",
    )

    assert result.success is True
    assert result.data["service_port"] == 401
    assert result.ont_id == 7
    assert failure.success is False
    assert failure.status == AdapterStatus.error
    assert failure.error_code == "RuntimeError"


def test_olt_result_projects_sanitized_huawei_response_evidence() -> None:
    from app.services.network.olt_protocol_adapters import OltOperationResult

    result = OltOperationResult(
        success=False,
        message="% Unknown command secret-value",
    )

    assert result.error_code == "unknown_command"
    assert result.data["huawei_cli_response"]["response_code"] == "unknown_command"
    assert result.data["huawei_cli_response"]["unsupported"] is True
    assert "secret-value" not in repr(result.data)


def test_olt_result_preserves_explicit_transport_error_code() -> None:
    from app.services.network.olt_protocol_adapters import OltOperationResult

    result = OltOperationResult(
        success=False,
        message="% Unknown command",
        error_code="TimeoutError",
    )

    assert result.error_code == "TimeoutError"
    assert result.data["huawei_cli_response"]["response_code"] == "unknown_command"


def test_ssh_iphost_timeout_uses_shared_error_result(monkeypatch) -> None:
    from app.services.network.olt_protocol_adapters import OltProtocolAdapter

    def timeout(*_args, **_kwargs):
        raise TimeoutError("OLT SSH timed out")

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.iphost.configure_ont_iphost",
        timeout,
    )

    adapter = OltProtocolAdapter(SimpleNamespace(name="OLT 1"))
    result = adapter.configure_iphost(
        "0/1/3",
        5,
        vlan=203,
        mode="dhcp",
    )

    assert result.success is False
    assert result.error_code == "TimeoutError"
    assert "SSH IPHOST configuration failed" in result.message
    assert "OLT SSH timed out" in result.message


def test_olt_protocol_adapter_passes_iphost_ip_index(monkeypatch) -> None:
    from app.services.network.olt_protocol_adapters import OltProtocolAdapter

    calls = []

    def configure(*_args, **kwargs):
        calls.append(kwargs)
        return True, "configured"

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.iphost.configure_ont_iphost",
        configure,
    )

    adapter = OltProtocolAdapter(SimpleNamespace(name="OLT 1"))
    result = adapter.configure_iphost(
        "0/1/3",
        5,
        ip_index=1,
        vlan=203,
        mode="dhcp",
    )

    assert result.success is True
    assert calls[0]["ip_index"] == 1


def test_olt_protocol_adapter_satisfies_contract() -> None:
    from app.services.network.olt_protocol_adapters import (
        OltProtocolAdapter,
        OltProtocolAdapterContract,
    )

    adapter = OltProtocolAdapter(SimpleNamespace(name="OLT 1"))

    assert isinstance(adapter, OltProtocolAdapterContract)


def test_adapter_registry_tracks_named_adapters() -> None:
    from app.services.adapters import AdapterRegistry

    class FakeAdapter:
        name = "fake"

    registry = AdapterRegistry()
    adapter = FakeAdapter()

    assert registry.register(adapter) is adapter
    assert registry.get("fake") is adapter
    assert registry.require("fake") is adapter
    assert registry.names() == ("fake",)


def test_global_adapter_registry_contains_core_singletons() -> None:
    from app.services import (
        audit_adapter,
        billing_adapter,
        db_session_adapter,
        external_bss_adapter,
        ipam_adapter,
        notification_adapter,
        olt_action_adapter,
        olt_detail_adapter,
        olt_observed_state_adapter,
        olt_profile_adapter,
        payment_gateway_adapter,
        queue_adapter,
        queue_strategy_adapter,
        rate_limiter_adapter,
        service_intent_ui_adapter,
    )
    from app.services.adapters import adapter_registry

    expected = {
        "audit": audit_adapter.audit_adapter,
        "billing": billing_adapter.billing_adapter,
        "db.session.sqlalchemy": db_session_adapter.db_session_adapter,
        "external_bss": external_bss_adapter.external_bss_adapter,
        "ipam": ipam_adapter.ipam_adapter,
        "notification": notification_adapter.notify,
        "olt_action": olt_action_adapter.olt_action_adapter,
        "olt_detail": olt_detail_adapter.olt_detail_adapter,
        "olt.observed_state": olt_observed_state_adapter.olt_observed_state_adapter,
        "olt_profile": olt_profile_adapter.olt_profile_adapter,
        "payment_gateway": payment_gateway_adapter.payment_gateway_adapter,
        "queue.celery": queue_adapter.queue_adapter,
        "queue.strategy": queue_strategy_adapter.queue_strategy,
        "rate_limiter.redis": rate_limiter_adapter.rate_limiter_adapter,
        "service_intent.ui": service_intent_ui_adapter.service_intent_ui_adapter,
    }

    for name, adapter in expected.items():
        assert adapter_registry.require(name) is adapter


def test_operation_result_converts_to_shared_adapter_result() -> None:
    from app.services.adapters.base import AdapterStatus
    from app.services.network.result_adapter import OperationResult, ResultStatus

    operation = OperationResult.queued(
        "queued",
        data={"operation_id": "op-1"},
    )

    shared = operation.to_adapter_result()
    round_trip = OperationResult.from_adapter_result(shared)

    assert shared.success is True
    assert shared.status == AdapterStatus.queued
    assert shared.data["operation_id"] == "op-1"
    assert round_trip.status == ResultStatus.queued
    assert round_trip.message == "queued"


def test_operation_result_preserves_adapter_error_code() -> None:
    from app.services.adapters.base import AdapterResult
    from app.services.network.result_adapter import OperationResult

    result = OperationResult.from_adapter_result(
        AdapterResult(
            success=False,
            message="rejected",
            error_code="unknown_command",
        )
    )

    assert result.data == {"error_code": "unknown_command"}


def test_rate_limiter_adapter_blocks_after_limit_until_window_resets() -> None:
    from app.services.rate_limiter_adapter import (
        InMemoryRateLimiterAdapter,
        RateLimitRule,
    )

    adapter = InMemoryRateLimiterAdapter()
    rule = RateLimitRule(key="olt:1", limit=2, window_seconds=60)
    now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)

    first = adapter.check(rule, now=now)
    second = adapter.check(rule, now=now + timedelta(seconds=1))
    blocked = adapter.check(rule, now=now + timedelta(seconds=2))
    reset = adapter.check(rule, now=now + timedelta(seconds=61))

    assert first.allowed is True
    assert second.allowed is True
    assert blocked.allowed is False
    assert blocked.retry_after_seconds is not None
    assert reset.allowed is True


def test_audit_adapter_builds_audit_payload() -> None:
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import AuditActor, AuditAdapter, AuditRecord

    actor_party_id = uuid4()

    payload = AuditAdapter().build_payload(
        AuditRecord(
            action="provision",
            entity_type="ont",
            entity_id="ont-1",
            actor=AuditActor.user(
                "operator-1",
                party_id=actor_party_id,
                label="Network operator",
            ),
            metadata={"result": "ok"},
            status_code=200,
            request_id="req-1",
        )
    )

    assert payload.action == "provision"
    assert payload.entity_type == "ont"
    assert payload.actor_type == AuditActorType.user
    assert payload.actor_id == "operator-1"
    assert payload.actor_party_id == actor_party_id
    assert payload.metadata_ == {"result": "ok"}
    assert payload.request_id == "req-1"


def test_audit_actor_rejects_party_identity_for_automated_actors() -> None:
    import pytest

    from app.models.audit import AuditActorType
    from app.services.audit_adapter import AuditActor

    with pytest.raises(ValueError, match="cannot carry a Party identity"):
        AuditActor(actor_type=AuditActorType.system, party_id=uuid4())
    with pytest.raises(ValueError, match="cannot carry a Party identity"):
        AuditActor(
            actor_type=AuditActorType.service,
            actor_id="provisioner",
            party_id=uuid4(),
        )


def test_audit_actor_rejects_an_unidentified_principal() -> None:
    import pytest

    from app.models.audit import AuditActorType
    from app.services.audit_adapter import AuditActor

    with pytest.raises(ValueError, match="needs a non-empty actor_id"):
        AuditActor(actor_type=AuditActorType.user, actor_id="  ")


def test_billing_adapter_builds_invoice_and_payment_payloads(monkeypatch) -> None:
    from app.models.billing import InvoiceDueDateBasis, InvoiceStatus, PaymentStatus
    from app.services.billing_adapter import (
        BillingAdapter,
        InvoiceIntent,
        PaymentIntent,
    )

    account_id = uuid4()
    captured = {}

    class FakeInvoices:
        @staticmethod
        def create(db, payload):
            captured["invoice"] = payload
            return payload

    class FakePayments:
        @staticmethod
        def create(db, payload):
            captured["payment"] = payload
            return payload

    fake_billing = SimpleNamespace(invoices=FakeInvoices(), payments=FakePayments())
    adapter = BillingAdapter(billing_service=fake_billing)
    issued_at = datetime.now(UTC)
    invoice = adapter.create_invoice(
        None,
        InvoiceIntent(
            account_id=account_id,
            invoice_number="INV-1",
            total=Decimal("150.00"),
            status=InvoiceStatus.issued,
            issued_at=issued_at,
            due_at=issued_at + timedelta(days=30),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test-contract",
            due_date_policy_version="test-v1",
        ),
    )
    payment = adapter.record_payment(
        None,
        PaymentIntent(
            account_id=account_id,
            amount=Decimal("150.00"),
            external_id="GW-1",
            status=PaymentStatus.succeeded,
        ),
    )

    assert invoice.invoice_number == "INV-1"
    assert invoice.balance_due == Decimal("150.00")
    assert payment.external_id == "GW-1"
    assert payment.status == PaymentStatus.succeeded


def test_billing_adapter_uses_default_currency_when_intent_omits_currency(
    monkeypatch,
) -> None:
    from app.services.billing_adapter import (
        BillingAdapter,
        InvoiceIntent,
        PaymentIntent,
    )

    account_id = uuid4()
    captured = {}

    class FakeInvoices:
        @staticmethod
        def create(db, payload):
            captured["invoice"] = payload
            return payload

    class FakePayments:
        @staticmethod
        def create(db, payload):
            captured["payment"] = payload
            return payload

    fake_billing = SimpleNamespace(invoices=FakeInvoices(), payments=FakePayments())
    adapter = BillingAdapter(billing_service=fake_billing)
    monkeypatch.setattr(
        "app.services.settings_spec.resolve_value",
        lambda *_args, **_kwargs: "USD",
    )

    adapter.create_invoice(
        object(),
        InvoiceIntent(account_id=account_id, total=Decimal("150.00")),
    )
    adapter.record_payment(
        object(),
        PaymentIntent(account_id=account_id, amount=Decimal("150.00")),
    )

    assert captured["invoice"].currency == "USD"
    assert captured["payment"].currency == "USD"


def test_external_bss_adapter_builds_reference_payload() -> None:
    from app.models.external import ExternalEntityType
    from app.services.external_bss_adapter import (
        ExternalBssAdapter,
        ExternalBssReference,
    )

    connector_id = uuid4()
    entity_id = uuid4()
    payload = ExternalBssAdapter().build_reference_payload(
        ExternalBssReference(
            connector_config_id=connector_id,
            entity_type=ExternalEntityType.subscriber,
            entity_id=entity_id,
            external_id="splynx-123",
            external_url="https://bss.example/customers/123",
            metadata={"source": "splynx"},
        )
    )

    assert payload.connector_config_id == connector_id
    assert payload.entity_type == ExternalEntityType.subscriber
    assert payload.entity_id == entity_id
    assert payload.external_id == "splynx-123"
    assert payload.metadata_ == {"source": "splynx"}


def test_redis_rate_limiter_falls_back_to_in_memory_when_redis_down(monkeypatch):
    from app.services import rate_limiter_adapter as rl

    # Redis unavailable → per-worker in-memory limiter still throttles.
    monkeypatch.setattr(rl, "InMemoryRateLimiterAdapter", rl.InMemoryRateLimiterAdapter)
    import app.services.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis", lambda *a, **k: None)

    adapter = rl.RedisRateLimiterAdapter(rl.InMemoryRateLimiterAdapter())
    rule = rl.RateLimitRule(key="test:fallback", limit=2, window_seconds=900)
    assert adapter.check(rule).allowed is True
    assert adapter.check(rule).allowed is True
    denied = adapter.check(rule)
    assert denied.allowed is False
    assert denied.retry_after_seconds and denied.retry_after_seconds > 0


def test_redis_rate_limiter_uses_shared_counter(monkeypatch):
    from app.services import rate_limiter_adapter as rl

    class _FakeRedis:
        def __init__(self):
            self.store = {}
            self.expires = {}

        def incr(self, key):
            self.store[key] = self.store.get(key, 0) + 1
            return self.store[key]

        def expire(self, key, seconds):
            self.expires[key] = seconds
            return True

    fake = _FakeRedis()
    import app.services.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis", lambda *a, **k: fake)

    adapter = rl.RedisRateLimiterAdapter(rl.InMemoryRateLimiterAdapter())
    rule = rl.RateLimitRule(key="test:shared", limit=3, window_seconds=900)
    # A second adapter instance (simulating another worker) shares the counter.
    other = rl.RedisRateLimiterAdapter(rl.InMemoryRateLimiterAdapter())
    assert adapter.check(rule).allowed is True
    assert other.check(rule).allowed is True
    assert adapter.check(rule).allowed is True
    assert other.check(rule).allowed is False  # 4th hit across "workers"
    # EXPIRE set exactly once, on the first hit.
    assert len(fake.expires) == 1
