# Admin System Settings Atomicity

The admin System Settings form validates its complete submitted batch before
calling `control.settings_form_updates`. That owner stages every setting and a
value-free audit record, then commits once. Any validation or persistence
failure leaves every submitted setting unchanged and returns a form error.

Blank optional settings without a registered default are omitted. Blank secret
fields are also omitted so an existing secret remains unchanged. Boolean,
integer, list, JSON, string, allowed-value, and bound handling continues to come
from `control.settings_spec`.

This contract addresses incident `1adc8f17-a043-4eb3-9fcd-72d71e106e6b`, where
a blank optional integer failed after an earlier field had already committed.
Audit evidence contains setting domains, keys, and batch count only; it never
contains submitted values or secret material.
