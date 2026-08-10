from fastapi import HTTPException

from app.services import module_manager


def test_load_module_states_defaults_true_when_settings_missing(monkeypatch):
    # No cache to stub: the aggregate `modules:states` entry is gone. It was a
    # derived map living in the settings keyspace under an unscoped key, and
    # the per-flag reads it is rebuilt from are cached by the kernel now.
    def _missing(_db, _key):
        raise HTTPException(status_code=404, detail="missing")

    monkeypatch.setattr(
        module_manager.domain_settings_service.modules_settings, "get_by_key", _missing
    )
    states = module_manager.load_module_states(db=object(), force_refresh=True)
    assert states["network"] is True
    assert states["reports"] is True
    assert "billing" not in states


def test_load_module_states_always_rebuilds_from_the_resolver(monkeypatch):
    """Replaces `test_load_module_states_prefers_cached_value`.

    That test asserted the aggregate cache was preferred over a fresh read.
    There is no aggregate cache, so asserting the OPPOSITE is the honest
    replacement rather than deleting the coverage: `force_refresh=False` must
    still reflect what the settings say right now.
    """

    reads: list[str] = []
    # The SETTING key, from the map, not a guess at it: modules are named
    # "network" and their settings "module_network_enabled", and my first
    # version of this test compared against the wrong one and passed vacuously
    # on the default.
    network_key = module_manager.MODULE_KEY_MAP["network"]

    def _flag(_db, key, default=True):
        reads.append(key)
        return key != network_key

    monkeypatch.setattr(module_manager, "_resolve_module_flag", _flag)

    states = module_manager.load_module_states(db=object(), force_refresh=False)

    assert network_key in reads, "the flag was not read — a cache crept back in"
    assert states["network"] is False
    assert states["gis"] is True


def test_update_module_flags_upserts(monkeypatch):
    """Was `..._upserts_and_invalidates`.

    The invalidation half moved: writing a module flag writes a
    `DomainSetting`, and invalidation is now one `after_commit` listener on that
    model rather than a call here. What this function still owns is the upsert,
    so that is what it still asserts —
    `tests/test_settings_cache_invalidation.py` covers the listener.
    """

    upserts: list[tuple[str, bool]] = []

    def _fake_upsert(_db, key, enabled):
        upserts.append((key, enabled))

    monkeypatch.setattr(module_manager, "_upsert_boolean_setting", _fake_upsert)

    module_manager.update_module_flags(
        db=object(),
        payload={"network": False, "gis": True, "unknown": False},
    )

    assert ("module_network_enabled", False) in upserts
    assert ("module_gis_enabled", True) in upserts
    assert ("module_unknown_enabled", False) not in upserts


def test_load_feature_states_projects_permanent_customer_capability(db_session):
    states = module_manager.load_feature_states(db_session, force_refresh=True)

    assert states == {"services_view": True}


def _make_provider(db_session, name, provider_type, *, is_active=True):
    from app.models.billing import PaymentProvider

    provider = PaymentProvider(
        name=name, provider_type=provider_type, is_active=is_active
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)
    return provider


def test_list_payment_providers_returns_rows(db_session):
    from app.models.billing import PaymentProviderType

    _make_provider(db_session, "Paystack", PaymentProviderType.paystack)
    _make_provider(
        db_session, "Flutterwave", PaymentProviderType.flutterwave, is_active=False
    )

    providers = module_manager.list_payment_providers(db_session)

    by_name = {p["name"]: p for p in providers}
    assert by_name["Paystack"]["provider_type"] == "paystack"
    assert by_name["Paystack"]["is_active"] is True
    assert by_name["Flutterwave"]["is_active"] is False
    assert all("id" in p for p in providers)


def test_update_provider_flags_roundtrip(db_session):
    from app.models.billing import PaymentProvider, PaymentProviderType

    paystack = _make_provider(db_session, "Paystack", PaymentProviderType.paystack)
    flutter = _make_provider(
        db_session, "Flutterwave", PaymentProviderType.flutterwave, is_active=False
    )

    module_manager.update_provider_flags(
        db_session,
        payload={str(paystack.id): False, str(flutter.id): True},
    )

    assert db_session.get(PaymentProvider, paystack.id).is_active is False
    assert db_session.get(PaymentProvider, flutter.id).is_active is True


def test_update_provider_flags_skips_unknown_ids(db_session):
    # A malformed / unknown id must not raise.
    module_manager.update_provider_flags(db_session, payload={"not-a-uuid": False})


def test_module_manager_page_state_includes_providers(db_session):
    from app.models.billing import PaymentProviderType

    _make_provider(db_session, "Paystack", PaymentProviderType.paystack)

    state = module_manager.module_manager_page_state(db_session)

    assert "payment_providers" in state
    assert any(p["name"] == "Paystack" for p in state["payment_providers"])


def test_module_manager_page_state_excludes_financial_lifecycle_modules(db_session):
    state = module_manager.module_manager_page_state(db_session)

    module_names = {card["name"] for card in state["module_cards"]}
    assert {"billing", "catalog", "customer", "notifications"}.isdisjoint(module_names)
