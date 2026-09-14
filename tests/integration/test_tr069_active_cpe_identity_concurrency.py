"""PostgreSQL concurrency proof for active CPE/TR-069 identity."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.network import CPEDevice, DeviceStatus
from app.models.subscriber import Reseller, Subscriber
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice


def test_concurrent_activation_yields_one_active_tr069_identity(engine) -> None:
    """The database, not a timing-sensitive read, closes the activation race."""

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    unique = uuid.uuid4().hex[:12]
    with factory() as setup:
        reseller = Reseller(name=f"Identity Race {unique}", code=f"RACE-{unique}")
        subscriber = Subscriber(
            first_name="Identity",
            last_name="Race",
            email=f"identity-race-{unique}@example.com",
            reseller=reseller,
        )
        cpe = CPEDevice(
            subscriber=subscriber,
            status=DeviceStatus.active,
            serial_number=f"CPE-RACE-{unique}",
        )
        server = Tr069AcsServer(
            name=f"Identity Race ACS {unique}",
            base_url="http://acs.test.local",
        )
        first = Tr069CpeDevice(
            acs_server=server,
            cpe_device=cpe,
            serial_number=f"TR069-A-{unique}",
            is_active=False,
        )
        second = Tr069CpeDevice(
            acs_server=server,
            cpe_device=cpe,
            serial_number=f"TR069-B-{unique}",
            is_active=False,
        )
        setup.add_all([first, second])
        setup.commit()
        reseller_id = reseller.id
        subscriber_id = subscriber.id
        cpe_id = cpe.id
        server_id = server.id
        device_ids = (first.id, second.id)

    barrier = threading.Barrier(2)

    def activate(device_id: uuid.UUID) -> str:
        with factory() as session:
            device = session.get(Tr069CpeDevice, device_id)
            assert device is not None
            device.is_active = True
            barrier.wait(timeout=10)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return "conflict"
            return "activated"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(activate, device_id) for device_id in device_ids
            )
            outcomes = sorted(future.result(timeout=15) for future in futures)
        assert outcomes == ["activated", "conflict"]
        with factory() as verify:
            active_count = verify.scalar(
                select(func.count())
                .select_from(Tr069CpeDevice)
                .where(
                    Tr069CpeDevice.cpe_device_id == cpe_id,
                    Tr069CpeDevice.is_active.is_(True),
                )
            )
            assert active_count == 1
    finally:
        with factory() as cleanup:
            cleanup.execute(
                delete(Tr069CpeDevice).where(Tr069CpeDevice.id.in_(device_ids))
            )
            cleanup.execute(delete(CPEDevice).where(CPEDevice.id == cpe_id))
            cleanup.execute(delete(Subscriber).where(Subscriber.id == subscriber_id))
            cleanup.execute(delete(Reseller).where(Reseller.id == reseller_id))
            cleanup.execute(
                delete(Tr069AcsServer).where(Tr069AcsServer.id == server_id)
            )
            cleanup.commit()
