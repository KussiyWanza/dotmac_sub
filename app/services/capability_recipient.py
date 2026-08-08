"""The single authorised address a capability-bearing send may reach.

Customer communications and capability delivery answer different questions and
must not share a rule. A billing notice should reach the account holder *and*
whichever contacts they nominated. A password reset, credential enrollment or
email-verification link must reach exactly one authorised mailbox: it carries a
capability, so an extra recipient is an authorisation decision, not a courtesy.

Nothing owned that distinction. Every capability sender inlined
``to_email=subscriber.email`` and the effective recipient set was decided
downstream by the transport splitter in ``app.services.email`` — which exists to
serve customer communications, where fanning out is correct. A stored value of
``"a@x.com, b@y.com"`` therefore delivered credential links to both mailboxes.

This owner makes the rule explicit and fails closed. It deliberately does not
choose between addresses when a field holds several: picking the first would be
a silent guess about who is authorised, and picking wrong sends the capability
to the wrong person while telling nobody.
"""

from __future__ import annotations

import logging

from app.services.domain_errors import DomainError
from app.services.email import resolve_recipient_addresses

logger = logging.getLogger(__name__)

OWNER = "identity.capability_recipient"


class CapabilityRecipientError(DomainError):
    """No single authorised address could be resolved for a capability send."""


def resolve_capability_recipient(
    value: str | None,
    *,
    subject: str,
) -> str:
    """Return the one address this capability may be delivered to, or refuse.

    ``subject`` names what the address belongs to (for example
    ``"subscriber:<id>"``) so a refusal identifies the record to correct.
    """
    resolved = resolve_recipient_addresses(value)

    if len(resolved.deliverable) == 1:
        return resolved.deliverable[0]

    # Refusing is correct but silent on its own. These are records someone has
    # to go and correct, so each refusal names the subject and the reason at
    # WARNING, giving a single searchable term for the work queue.
    if not resolved.deliverable:
        logger.warning(
            "capability_recipient_unresolved: reason=no_deliverable_address "
            "subject=%s rejected=%d",
            subject,
            len(resolved.rejected),
        )
        raise CapabilityRecipientError(
            code=f"{OWNER}.no_deliverable_address",
            message=(
                "This record has no deliverable email address, so a capability "
                "link cannot be sent to it."
            ),
            details={"subject": subject, "rejected": list(resolved.rejected)},
        )

    logger.warning(
        "capability_recipient_unresolved: reason=ambiguous_address "
        "subject=%s address_count=%d",
        subject,
        len(resolved.deliverable),
    )
    raise CapabilityRecipientError(
        code=f"{OWNER}.ambiguous_address",
        message=(
            "This record holds more than one email address. A capability link "
            "is not sent until exactly one authorised address is recorded, "
            "because delivering it to every address would grant access to "
            "each of them."
        ),
        details={"subject": subject, "address_count": len(resolved.deliverable)},
    )


__all__ = [
    "CapabilityRecipientError",
    "OWNER",
    "resolve_capability_recipient",
]
