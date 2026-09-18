"""Governed work-order kind vocabulary."""

from enum import StrEnum


class WorkOrderKind(StrEnum):
    CUSTOMER = "customer"
    INFRASTRUCTURE = "infrastructure"
