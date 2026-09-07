"""Native infrastructure kinds shared by selectors and project relationships."""

from enum import StrEnum


class InfrastructureType(StrEnum):
    location = "location"
    nas = "nas"
    access_point = "access_point"
    base_station = "base_station"
    olt = "olt"
    pon_port = "pon_port"
    cabinet = "cabinet"
