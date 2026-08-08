# Notification channel policy

**Owner:** `app.services.notification_channel_policy` (`communications.channel_policy` in the SOT registry)
**Operator surface:** `/admin/notifications/channels`
**Stored as:** `domain_settings` row `notification` / `notification_channel_policy` (JSON)

## The rule

Which channels a customer notification goes out on is decided in exactly one
place. Callers state *intent* — template code, event type, category — plus their
own fallback, and the policy answers with an ordered channel tuple.

A feature area must not carry its own "delivery channel" setting. Two settings
that both claim to pick a channel cannot be reconciled, and in practice one of
them silently wins while operators believe they configured the other.

## Precedence

Resolution runs top-down and returns the first non-empty match:

1. `notification_event_<template_code>_channels` — legacy per-event setting
2. `notification_channel_policy.events[<template_code> | <event_type>]`
3. `notification_channel_policy.categories[<category>]`
4. `notification_channel_policy.default`
5. The caller's own defaults (the `channels=` tuple on `EventNotificationSpec`)

Level 1 outranks the admin page, so the page shows a banner and marks affected
rows `from legacy setting` rather than displaying a selection that has no
effect. Clearing those rows hands control back to the UI.

## Stored shape

```json
{
  "default": ["email"],
  "categories": {"service": ["whatsapp", "email"], "billing": ["whatsapp"]},
  "events": {"outage_area": ["whatsapp"]}
}
```

Empty selections are **dropped, not stored as empty lists**. An empty list would
read as "send on no channel"; omitting the key lets resolution fall through to
the next level.

## Selectable channels

`SELECTABLE_CHANNELS` = email, SMS, WhatsApp, push.

`webhook` and `websocket` remain in `NotificationChannel` because they are
transports used elsewhere, but they are not customer-reachable channels and are
rejected if submitted. WhatsApp was approved as a customer lifecycle channel on
2026-07-23; before that it was admin-campaign-only.

## Writing

`set_channel_policy()` is the only supported writer. It validates every
submitted channel against `SELECTABLE_CHANNELS` and raises `DomainError`
(`notification_channel_unsupported`) rather than silently discarding an unknown
value.

## Retired settings

These were UI-only controls with no runtime reader — an operator could set them
and nothing consumed the value. Removed in favour of the policy:

| Retired key | Was on |
|---|---|
| `reminder_channel` | System → Config → Billing Reminders |
| `blocking_wave_channel` | System → Config → Billing Notifications |
| `pre_block_wave1_channel` | System → Config → Billing Notifications |
| `pre_block_wave2_channel` | System → Config → Billing Notifications |

`tests/test_notification_channel_policy_admin.py` asserts they stay gone from
both the config key lists and the templates.

## Internal Nextcloud Talk delivery

`nextcloud_talk` is an internal staff transport and is intentionally excluded
from `SELECTABLE_CHANNELS`. Ticket/project assignment and explicit-mention
owners stage it through `communications.nextcloud_talk_staff`; customer channel
preferences and the customer channel policy do not apply. The feature flag
defaults off, while connector URL, notifier username, timeout, and the
app-password secret reference belong to the version-pinned integration
installation.

## Related

- `docs/SOT_RELATIONSHIP_MAP.md` — `notifications_communications` domain
- `app/services/events/handlers/notification.py` — `EVENT_NOTIFICATION_SPECS`
  and the `event_catalogue()` the admin page renders
