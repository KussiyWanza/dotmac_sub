"""Keep CRM business state read-only from Sub.

ADR 0006 temporarily permits identified widget-session minting as an
authentication transport. It creates no CRM business record by itself and is
retired with the temporary portal-chat capability.
"""

from __future__ import annotations

from pathlib import Path

from app.services.crm_client import CRMClient

ROOT = Path(__file__).resolve().parents[2]


#: Sub -> CRM business writes that are gone for good.
RETIRED_MUTATIONS = {
    "post_signed_webhook",
    "update_subscriber",
    "delete_subscriber",
    "create_ticket",
    "update_ticket",
    "create_ticket_comment",
    "update_work_order",
    "submit_portal_technician_rating",
}

#: The last remaining Sub -> CRM writes.
#:
#: Quote writes remain the write half of a cutover whose read half has not
#: happened. Referral reads/writes cut over to Sub in revision 356, so the CRM
#: referral mutation has been removed from this set.
#:
#: This set must only ever SHRINK. It goes to zero when the read cutover lands.
DEFERRED_MUTATIONS = {
    "request_portal_quote",
    "accept_portal_quote",
}


def test_retired_crm_mutations_are_gone() -> None:
    assert RETIRED_MUTATIONS.isdisjoint(dir(CRMClient))


def test_deferred_mutations_are_still_present_and_tracked() -> None:
    """The last Sub -> CRM writes must not be silently dropped OR silently kept.

    Removing one without doing the read cutover would break portal quote
    requests and referrals -- the mirrors are still the read source. When the
    cutover lands these disappear and this set goes to zero, and that deletion
    is the visible signal that it happened.
    """
    assert DEFERRED_MUTATIONS <= set(dir(CRMClient))


def test_no_new_crm_business_writes_are_added() -> None:
    """Guard against a new Sub -> CRM write creeping in.

    Portal and temporary widget session minting are deliberately exempt: they
    mint authentication sessions, not business state. CRM business records
    remain read-only from Sub.
    """
    write_verbs = (
        "create_",
        "update_",
        "delete_",
        "submit_",
        "post_",
        "accept_",
        "request_",
    )
    non_mutations = {"create_portal_session", "create_widget_session"}
    present = {
        name
        for name in dir(CRMClient)
        if not name.startswith("_") and name.startswith(write_verbs)
    }
    assert present - non_mutations == DEFERRED_MUTATIONS, (
        "CRMClient's write surface changed. Expected only the deferred portal "
        f"writes, found: {sorted(present - non_mutations)}"
    )


def test_outbound_crm_tasks_are_not_registered() -> None:
    sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "app/celery_app.py",
            "app/tasks/__init__.py",
            "app/services/scheduler_config.py",
            "app/services/events/dispatcher.py",
        )
    )
    for forbidden in (
        "push_ticket_to_crm",
        "push_comment_to_crm",
        "push_crm_billing_snapshots",
        "push_subscriber_change",
        "CrmSyncHandler",
    ):
        assert forbidden not in sources


def test_customer_referral_surfaces_do_not_call_crm_mirror() -> None:
    sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in ("app/api/me.py", "app/web/customer/referrals.py")
    )
    assert "referrals_mirror" not in sources


def test_referral_domain_has_no_crm_integration_path() -> None:
    mirror_source = (ROOT / "app/services/referrals_mirror.py").read_text(
        encoding="utf-8"
    )
    webhook_source = (ROOT / "app/api/crm_webhooks.py").read_text(encoding="utf-8")

    for forbidden in (
        "get_crm_client",
        "resolve_crm_subscriber_id",
        "reconcile_subscriber",
        "apply_webhook",
        "enqueue_task",
    ):
        assert forbidden not in mirror_source
    assert "referral" not in webhook_source.lower()
    assert not (ROOT / "app/services/crm_native_sync.py").exists()
