from types import SimpleNamespace
from uuid import uuid4

from app.services import provisioning_context, provisioning_helpers
from app.services.network.subscriber_ont_adapter import ProvisioningContext
from app.services.provisioning_step_executors import execute_create_olt_service_port


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None


class _EmptyScalars:
    def first(self):
        return None

    def all(self):
        return []


class _FakeSession:
    def __init__(self, subscription):
        self.subscription = subscription

    def get(self, model, key):
        if model.__name__ == "Subscription":
            return self.subscription
        return None

    def query(self, *args, **kwargs):
        return _EmptyQuery()

    def scalars(self, *args, **kwargs):
        """The kernel resolver's session API, which is not `query`.

        Settings resolution moved to `dotmac_kernel.settings_resolver`, which
        issues `db.scalars(select(...)).first()`. A fake shaped for the old
        `db.query(...)` chain raises AttributeError here — loudly, which is the
        good case. A `MagicMock` in the same position does NOT: it returns
        another mock and the test asserts against it, which is how two
        scheduler-config tests started comparing a mock to "Africa/Lagos".

        No stored settings rows: every read falls through to its spec default,
        which is what this test wants.
        """

        return _EmptyScalars()


def test_extend_provisioning_context_merges_canonical_network_context(monkeypatch):
    subscriber_id = uuid4()
    subscription_id = uuid4()
    resolved_ont_id = uuid4()
    nas_device_id = uuid4()
    service_address_id = uuid4()
    db = _FakeSession(SimpleNamespace(id=subscription_id, subscriber_id=subscriber_id))

    def fake_resolve(db_arg, *, subscriber_id=None, subscription_id=None, ont_id=None):
        assert db_arg is db
        assert subscriber_id == str(subscriber_id_param)
        assert subscription_id == str(subscription_id_param)
        return ProvisioningContext(
            subscriber_id=str(subscriber_id_param),
            subscription_id=str(subscription_id_param),
            ont_id=str(resolved_ont_id),
            ont_serial="ONT123",
            olt_id=str(uuid4()),
            olt_name="OLT-A",
            fsp="0/1/2",
            ont_id_on_olt=17,
            service_address_id=str(service_address_id),
            nas_device_id=str(nas_device_id),
        )

    subscriber_id_param = subscriber_id
    subscription_id_param = subscription_id
    monkeypatch.setattr(
        provisioning_context,
        "resolve_operations_provisioning_context",
        fake_resolve,
    )

    context = {}

    provisioning_context.extend_provisioning_context(
        db,
        str(subscription_id),
        context,
    )

    assert context["subscriber_id"] == str(subscriber_id)
    assert context["subscription_id"] == str(subscription_id)
    assert context["ont_id"] == str(resolved_ont_id)
    assert context["ont_unit_id"] == str(resolved_ont_id)
    assert context["nas_device_id"] == str(nas_device_id)
    assert context["service_address_id"] == str(service_address_id)
    assert context["fsp"] == "0/1/2"
    assert context["ont_id_on_olt"] == 17


def test_legacy_extend_provisioning_context_delegates(monkeypatch):
    calls = {}

    def fake_extend(db, subscription_id, context):
        calls["args"] = (db, subscription_id, context)
        context["delegated"] = True
        return context

    monkeypatch.setattr(
        provisioning_helpers, "extend_provisioning_context", fake_extend
    )
    context = {}
    result = provisioning_helpers._extend_provisioning_context(
        object(),
        "subscription-1",
        context,
    )

    assert result is context
    assert context["delegated"] is True
    assert calls["args"][1] == "subscription-1"


def test_olt_service_port_executor_uses_operations_context(monkeypatch):
    def fake_resolve(db, *, subscriber_id=None, subscription_id=None, ont_id=None):
        return SimpleNamespace(ont_id="ont-1")

    monkeypatch.setattr(
        provisioning_context,
        "resolve_operations_provisioning_context",
        fake_resolve,
    )

    result = execute_create_olt_service_port(
        object(),
        {"subscription_id": "subscription-1"},
        {},
    )

    assert result.status == "failed"
    assert result.detail == (
        "VLAN ID is required in step config for OLT service-port creation."
    )


def test_provisioning_context_selects_ont_by_exact_subscription(
    db_session, subscriber, catalog_offer
):
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.network import OntAssignment, OntUnit
    from app.services.network.subscriber_ont_adapter import (
        resolve_provisioning_context,
    )

    first_subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
    )
    second_subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
    )
    first_ont = OntUnit(serial_number="PROV-SUB-ONE", is_active=True)
    second_ont = OntUnit(serial_number="PROV-SUB-TWO", is_active=True)
    db_session.add_all([first_subscription, second_subscription, first_ont, second_ont])
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=first_ont.id,
                subscriber_id=subscriber.id,
                subscription_id=first_subscription.id,
                active=True,
            ),
            OntAssignment(
                ont_unit_id=second_ont.id,
                subscriber_id=subscriber.id,
                subscription_id=second_subscription.id,
                active=True,
            ),
        ]
    )
    db_session.flush()

    context = resolve_provisioning_context(
        db_session,
        subscriber_id=str(subscriber.id),
        subscription_id=str(second_subscription.id),
    )

    assert context.subscription_id == str(second_subscription.id)
    assert context.ont_id == str(second_ont.id)
