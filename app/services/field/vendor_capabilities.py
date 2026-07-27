"""Declaring owner for what a vendor user may do inside their organisation.

The vendor portal previously had authentication and no authorization: every
authenticated vendor user could quote, submit as-built evidence, raise
purchase invoices, and drive project lifecycle, scoped only by which
``FieldVendor`` they resolved through. ``FieldVendorUser.role`` existed but was
free text that nothing ever read for a decision.

This module makes the role mean something. Two rules shape it:

* **Vendors are not staff.** Capability is resolved from the vendor membership,
  never from ``SystemUserRole``/``SystemUserPermission``. An external
  contractor must never appear on the staff authorization plane, so
  ``require_permission`` is deliberately not reused here.
* **Naming follows the same granular ``domain:resource:action`` convention**
  staff permissions use, so a reader can tell what a route gates on without
  learning a second vocabulary.

Capability is one half of the check. The other half — *which* project, quote or
invoice this vendor may touch — stays with the domain owners, which scope every
read and command by vendor id. Capability answers "may this person quote?";
the owner answers "may this vendor quote *this*?".

This module is transport-neutral by design: it decides, it does not raise HTTP.
``app.api.vendor_portal`` turns a refusal into a 403 dependency and
``app.web.vendor_portal`` into a 403 response.
"""

from __future__ import annotations

from app.models.field_vendor import DEFAULT_VENDOR_USER_ROLE, VENDOR_USER_ROLES

# --- capability vocabulary -------------------------------------------------

PROJECT_READ = "vendor:project:read"
PROJECT_EXECUTE = "vendor:project:execute"
QUOTE_WRITE = "vendor:quote:write"
AS_BUILT_WRITE = "vendor:as_built:write"
INVOICE_WRITE = "vendor:invoice:write"
MATERIAL_REQUEST = "vendor:material:request"
ADVANCE_REQUEST = "vendor:advance:request"

VENDOR_CAPABILITIES = (
    PROJECT_READ,
    PROJECT_EXECUTE,
    QUOTE_WRITE,
    AS_BUILT_WRITE,
    INVOICE_WRITE,
    MATERIAL_REQUEST,
    ADVANCE_REQUEST,
)

# Money out (quoting, invoicing) concentrates with the owner; evidence and
# execution spread to whoever is actually on site. A field technician can
# record what happened and never commit the organisation to a price.
_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "owner": frozenset(VENDOR_CAPABILITIES),
    # A supervisor runs the site: they can draw down material they need to
    # keep working, but asking Dotmac for money is the owner's call.
    "supervisor": frozenset(
        {
            PROJECT_READ,
            PROJECT_EXECUTE,
            QUOTE_WRITE,
            AS_BUILT_WRITE,
            MATERIAL_REQUEST,
        }
    ),
    "field": frozenset({PROJECT_READ, PROJECT_EXECUTE, AS_BUILT_WRITE}),
}

assert set(_ROLE_CAPABILITIES) == set(VENDOR_USER_ROLES), (
    "every declared vendor role needs a capability set"
)


def normalize_role(role: str | None) -> str:
    """Resolve a stored role to a known one.

    Imported and legacy rows carry arbitrary strings. An unrecognised role
    resolves to the least-privileged role rather than to nothing, so a bad
    value cannot silently grant more than intended.
    """
    candidate = (role or "").strip().lower()
    return candidate if candidate in _ROLE_CAPABILITIES else DEFAULT_VENDOR_USER_ROLE


def capabilities_for_role(role: str | None) -> frozenset[str]:
    return _ROLE_CAPABILITIES[normalize_role(role)]


def capabilities_for(context: dict) -> frozenset[str]:
    """Capability set for an authenticated vendor context."""
    return capabilities_for_role(context.get("vendor_role"))


def has_capability(context: dict, capability: str) -> bool:
    return capability in capabilities_for(context)


def assert_known_capability(capability: str) -> str:
    """Guard against a transport gating on a capability that does not exist.

    A typo would otherwise produce a route nobody can ever reach, or worse, a
    check that silently passes. Adapters call this when wiring their gates.
    """
    if capability not in VENDOR_CAPABILITIES:
        raise ValueError(f"unknown vendor capability {capability!r}")
    return capability


def refusal_message(capability: str) -> str:
    """The one refusal wording, so both transports say the same thing."""
    return f"Your vendor role does not allow this action ({capability})."
