"""Separate report assertion, subject and display preference vocabularies."""

from enum import StrEnum


SCHEMA_VERSION = "reported-cancer-1"


class Assertion(StrEnum):
    AFFIRMED = "AFFIRMED"
    NEGATED = "NEGATED"
    UNCERTAIN = "UNCERTAIN"
    UNKNOWN = "UNKNOWN"


class Subject(StrEnum):
    CURRENT_PRIMARY = "CURRENT_PRIMARY"
    METASTATIC_SITE = "METASTATIC_SITE"
    HISTORICAL = "HISTORICAL"
    OTHER_PERSON = "OTHER_PERSON"
    UNKNOWN = "UNKNOWN"


class SelectionMode(StrEnum):
    AUTO = "AUTO"
    GENERAL = "GENERAL"
    CANDIDATE = "CANDIDATE"
    MANUAL_PROFILE = "MANUAL_PROFILE"
