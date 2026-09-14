from app.models.billing import Invoice, Payment, PaymentProviderEvent
from app.models.communication_log import CommunicationLog
from app.models.network import OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.notification import NotificationDelivery
from app.models.subscriber import Subscriber
from app.models.tr069 import Tr069CpeDevice


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def test_tr069_external_identity_indexes_present():
    assert "uq_tr069_cpe_devices_active_genieacs_device_id" in _index_names(
        Tr069CpeDevice.__table__
    )
    assert "uq_tr069_cpe_devices_active_cpe_device_id" in _index_names(
        Tr069CpeDevice.__table__
    )


def test_splynx_identity_indexes_present():
    assert "uq_subscribers_splynx_customer_id" in _index_names(Subscriber.__table__)
    assert "uq_invoices_active_splynx_invoice_id" in _index_names(Invoice.__table__)
    assert "uq_payments_active_splynx_payment_id" in _index_names(Payment.__table__)
    assert "uq_network_devices_active_splynx_monitoring_id" in _index_names(
        NetworkDevice.__table__
    )


def test_payment_and_message_external_identity_indexes_present():
    assert "uq_payments_active_external_id" in _index_names(Payment.__table__)
    assert "uq_payment_provider_events_external_id" in _index_names(
        PaymentProviderEvent.__table__
    )
    assert "uq_notification_deliveries_provider_message" in _index_names(
        NotificationDelivery.__table__
    )
    assert "uq_communication_logs_channel_external_id" in _index_names(
        CommunicationLog.__table__
    )
    assert "uq_communication_logs_channel_splynx_message_id" in _index_names(
        CommunicationLog.__table__
    )


def test_ont_external_identity_index_is_declared_in_model():
    assert "uq_ont_units_olt_external_id" in _index_names(OntUnit.__table__)
