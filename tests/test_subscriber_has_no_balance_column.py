"""``Subscriber`` gaining a real ``balance`` column must be a deliberate act.

Display code (``web_billing_accounts``, historically ``web_billing_payments``)
has repeatedly reached for ``account.balance``/``getattr(account, "balance",
0)`` as if it were a real persisted field, when it never has been — the
account's actual open balance is derived from ``Invoice.balance_due`` at read
time. This canary pins the absence so that if ``Subscriber`` is ever given a
genuine ``balance`` column, it collides visibly with this test instead of
silently agreeing with unrelated display code that happens to read the same
attribute name.
"""

from __future__ import annotations

from app.models.subscriber import Subscriber


def test_subscriber_model_has_no_balance_attribute() -> None:
    assert not hasattr(Subscriber, "balance")
