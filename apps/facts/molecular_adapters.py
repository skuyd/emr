"""Lossless application adapters; published assay dates keep their old values."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from . import molecular_contracts
from .molecular_schema import DATE_KEYS


def molecular_field_content(key, value, raw_value=None, **kwargs):
    from .clinical_schema import field_content

    if key in DATE_KEYS:
        molecular_contracts.validate_value(key, value)
        if raw_value is not None and raw_value != value["raw"]:
            raise ValidationError("日期原文与字段原文外层不一致，请保留真实原文。")
        return field_content(key, {"value": value["value"], "precision": value["precision"]}, value["raw"], **kwargs)
    if raw_value is None:
        raw_value = value.get("raw", value.get("text")) if isinstance(value, dict) else None
    return field_content(key, deepcopy(value), raw_value, **kwargs)


def molecular_value(key, content):
    value = deepcopy(content["value"])
    if key in DATE_KEYS:
        value["raw"] = content["raw_value"]
        molecular_contracts.validate_value(key, value)
    return value
