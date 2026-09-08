"""Stable display order after existing selection and clinical calculations."""

from dataclasses import replace
from types import MappingProxyType


PROFILE_VERSION = "reported-display-groups-1"
PROFILES = MappingProxyType({
    "GENERAL": (),
    "LUNG": ("LAB_CEA", "LAB_CYFRA21_1", "LAB_NSE", "LAB_PROGRP"),
    "PANCREAS": ("LAB_CA19_9", "LAB_CEA"),
})
PROFILE_LABELS = MappingProxyType({"GENERAL": "通用顺序", "LUNG": "肺癌指标顺序", "PANCREAS": "胰腺癌指标顺序"})


def _ranks(profile):
    if profile not in PROFILES:
        raise ValueError("请选择已有的指标显示顺序。")
    return {code: index for index, code in enumerate(PROFILES[profile])}


def prioritize(items, profile, *, code_of=lambda item: item.standard_code):
    """Pure stable permutation: no filtering, normalization, copying or deduplication."""
    ranks = _ranks(profile)
    values = tuple(items)
    if not ranks:
        return values
    return tuple(sorted(values, key=lambda item: ranks.get(code_of(item), len(ranks))))


def prioritize_groups(groups, profile):
    """Move the actual visible group too; preserve empty and unprioritized groups."""
    ranks = _ranks(profile)
    values = tuple(groups)
    if not ranks:
        return values
    ordered = []
    for group in values:
        rows = prioritize(group.rows, profile)
        ordered.append(group if rows == group.rows else replace(group, rows=rows))
    return tuple(sorted(ordered, key=lambda group: min(
        (ranks.get(row.standard_code, len(ranks)) for row in group.rows), default=len(ranks),
    )))
