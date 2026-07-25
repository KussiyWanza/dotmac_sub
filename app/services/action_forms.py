"""Code-native contracts for server-owned action forms.

Domain command services remain authoritative for authorization, eligibility,
validation, and execution.  This module owns only the read-side interaction
contract rendered by web or mobile clients: action visibility, impact and
confirmation copy, declared fields, submitted values, and structured errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum


class ActionFieldKind(StrEnum):
    """Supported field controls for the first shared action-form renderer."""

    decimal = "decimal"
    select = "select"
    text = "text"
    textarea = "textarea"


class ActionMethod(StrEnum):
    """Mutation methods accepted by the action-form contract."""

    post = "post"


class ActionTone(StrEnum):
    """Semantic action role; concrete colors remain branding-owned."""

    positive = "positive"
    negative = "negative"
    neutral = "neutral"


@dataclass(frozen=True, slots=True)
class ActionOption:
    value: str
    label: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("Action option value is required")
        if not self.label.strip():
            raise ValueError("Action option label is required")


@dataclass(frozen=True, slots=True)
class ActionField:
    key: str
    label: str
    kind: ActionFieldKind
    value: str = ""
    required: bool = False
    help_text: str | None = None
    placeholder: str | None = None
    min_value: str | None = None
    step: str | None = None
    max_length: int | None = None
    rows: int | None = None
    options: tuple[ActionOption, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Action field key is required")
        if not self.label.strip():
            raise ValueError("Action field label is required")
        if self.max_length is not None and self.max_length < 1:
            raise ValueError("Action field max_length must be positive")
        if self.rows is not None and self.rows < 1:
            raise ValueError("Action field rows must be positive")
        option_values = tuple(option.value for option in self.options)
        if len(set(option_values)) != len(option_values):
            raise ValueError(f"Duplicate options for action field: {self.key}")
        if self.kind is ActionFieldKind.select and not self.options:
            raise ValueError(f"Select action field needs options: {self.key}")
        if self.kind is not ActionFieldKind.select and self.options:
            raise ValueError(f"Only select action fields accept options: {self.key}")


@dataclass(frozen=True, slots=True)
class ActionConfirmation:
    title: str
    message: str

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.message.strip():
            raise ValueError("Action confirmation title and message are required")


@dataclass(frozen=True, slots=True)
class ActionHiddenValue:
    """One owner-produced value carried back to a mutation adapter."""

    key: str
    value: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Action hidden-value key is required")
        if not self.value.strip():
            raise ValueError(f"Action hidden value is required: {self.key}")


@dataclass(frozen=True, slots=True)
class ActionFormSubmission:
    """Normalized submitted values and errors for exactly one action."""

    action_key: str
    values: tuple[tuple[str, str], ...]
    field_errors: tuple[tuple[str, str], ...] = ()
    general_error: str | None = None

    @classmethod
    def from_mapping(
        cls,
        action_key: str,
        values: Mapping[str, object | None],
        *,
        field_errors: Mapping[str, str] | None = None,
        general_error: str | None = None,
    ) -> ActionFormSubmission:
        normalized_values = tuple(
            (str(key), "" if value is None else str(value))
            for key, value in values.items()
        )
        normalized_errors = tuple(
            (str(key), str(message))
            for key, message in (field_errors or {}).items()
            if str(message).strip()
        )
        return cls(
            action_key=str(action_key),
            values=normalized_values,
            field_errors=normalized_errors,
            general_error=str(general_error).strip() if general_error else None,
        )

    def __post_init__(self) -> None:
        if not self.action_key.strip():
            raise ValueError("Action submission key is required")
        for name, pairs in (("values", self.values), ("errors", self.field_errors)):
            keys = tuple(key for key, _ in pairs)
            if len(set(keys)) != len(keys):
                raise ValueError(
                    f"Duplicate action submission {name}: {self.action_key}"
                )

    def restrict(self, field_keys: set[str]) -> ActionFormSubmission:
        """Keep only fields declared by one concrete action projection."""

        return replace(
            self,
            values=tuple(pair for pair in self.values if pair[0] in field_keys),
            field_errors=tuple(
                pair for pair in self.field_errors if pair[0] in field_keys
            ),
        )


@dataclass(frozen=True, slots=True)
class ActionForm:
    """One fully projected mutation form for a UI consumer."""

    key: str
    title: str
    description: str
    action_url: str
    submit_label: str
    fields: tuple[ActionField, ...]
    hidden_values: tuple[ActionHiddenValue, ...] = ()
    tone: ActionTone = ActionTone.neutral
    method: ActionMethod = ActionMethod.post
    impact: str | None = None
    confirmation: ActionConfirmation | None = None
    visible: bool = True
    allowed: bool = True
    disabled_reason: str | None = None
    general_error: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("key", self.key),
            ("title", self.title),
            ("description", self.description),
            ("action_url", self.action_url),
            ("submit_label", self.submit_label),
        ):
            if not value.strip():
                raise ValueError(f"Action form {name} is required")
        field_keys = tuple(field.key for field in self.fields)
        if len(set(field_keys)) != len(field_keys):
            raise ValueError(f"Duplicate fields in action form: {self.key}")
        hidden_keys = tuple(item.key for item in self.hidden_values)
        if len(set(hidden_keys)) != len(hidden_keys):
            raise ValueError(f"Duplicate hidden values in action form: {self.key}")
        reserved_keys = {"csrf_token", "confirmed"}
        collisions = (set(field_keys) & set(hidden_keys)) | (
            (set(field_keys) | set(hidden_keys)) & reserved_keys
        )
        if collisions:
            keys = ", ".join(sorted(collisions))
            raise ValueError(f"Reserved or duplicate action-form keys: {keys}")
        if (
            self.visible
            and not self.allowed
            and not (self.disabled_reason or "").strip()
        ):
            raise ValueError(
                f"Visible disabled action needs an explanation: {self.key}"
            )

    def bind(self, submission: ActionFormSubmission) -> ActionForm:
        """Return a copy carrying one failed submission's values and errors."""

        if submission.action_key != self.key:
            raise ValueError(
                f"Submission for {submission.action_key} cannot bind {self.key}"
            )
        field_keys = {field.key for field in self.fields}
        submitted_values = dict(submission.values)
        submitted_errors = dict(submission.field_errors)
        unknown_values = set(submitted_values) - field_keys
        unknown_errors = set(submitted_errors) - field_keys
        if unknown_values or unknown_errors:
            unknown = ", ".join(sorted(unknown_values | unknown_errors))
            raise ValueError(f"Undeclared fields submitted for {self.key}: {unknown}")
        return replace(
            self,
            fields=tuple(
                replace(
                    field,
                    value=submitted_values.get(field.key, field.value),
                    error=submitted_errors.get(field.key),
                )
                for field in self.fields
            ),
            general_error=submission.general_error,
        )

    def field(self, key: str) -> ActionField:
        for field in self.fields:
            if field.key == key:
                return field
        raise KeyError(key)
