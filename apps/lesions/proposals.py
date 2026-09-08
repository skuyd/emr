"""Explainable pending match proposals; never a medical confirmation.

Only explicit field equality contributes evidence. In particular, a reference
date alone, a similar measurement or a common organ does not identify a lesion.
Normalization here is for comparison only; original fields/spans are untouched.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
from itertools import combinations
import json
import unicodedata


RULE_VERSION = "explicit_location_candidates_v1"
UNCERTAIN = ("不能除外", "不除外", "可能", "不确定", "疑似", "无法确定", "难以确定")


@dataclass(frozen=True)
class MatchProposal:
    first_id: str
    second_id: str
    first_binding: dict
    second_binding: dict
    reasons: tuple
    blockers: tuple
    fingerprint: str
    rule_version: str = RULE_VERSION
    status: str = "PENDING"


def _normalized(value):
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _fields(row, key):
    return [field for field in row.get("fields", []) + row.get("context_fields", [])
            if field["field_key"] == key and field.get("source_valid", True)
            and field.get("status") not in {"EXCLUDED", "REVOKED"}]


def _values(row, key, attribute):
    return {_normalized(field["content"]["value"].get(attribute)) for field in _fields(row, key)
            if _normalized(field["content"]["value"].get(attribute))}


def _reason(code, *fields):
    return {"code": code, "field_ids": sorted({field["id"] for group in fields for field in group})}


def _day(field):
    value = field["content"]["value"]
    raw = value.get("value")
    if value.get("precision") != "DAY" or not isinstance(raw, str) or len(raw) != 10:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def _exam_day(row):
    fields = _fields(row, "report.exam_date")
    days = {_day(field) for field in fields}
    return next(iter(days)) if len(days) == 1 and None not in days and not any(
        field.get("conflict") for field in fields) else None


def _ordered(first, second):
    days = _exam_day(first), _exam_day(second)
    if all(days) and days[0] != days[1]:
        return (first, second) if days[0] < days[1] else (second, first)
    return tuple(sorted((first, second), key=lambda row: row["id"]))


def _pair(first, second):
    if (first["patient_id"] != second["patient_id"] or first["report_id"] == second["report_id"]
            or first["id"] == second["id"]):
        return None
    if not first.get("source_binding") or not second.get("source_binding") or any(
            row.get("status") == "UNAVAILABLE" for row in (first, second)):
        return None
    locations = [_values(row, "lesion.site", "text") for row in (first, second)]
    if not locations[0] & locations[1]:
        return None
    sides = [_values(row, "lesion.laterality", "code") - {"UNKNOWN"} for row in (first, second)]
    bodies = [_values(row, "imaging.body_site", "text") for row in (first, second)]
    if any(left and right and left.isdisjoint(right) for left, right in (sides, bodies)):
        return None
    first, second = _ordered(first, second)
    rows = (first, second)
    reasons = [_reason("explicit_location_equal", *[_fields(row, "lesion.site") for row in rows])]
    blockers = set()
    if any(len(values) != 1 for values in locations):
        blockers.add("ambiguous_location")
    if sides[0] and sides[0] == sides[1] and len(sides[0]) == 1:
        reasons.append(_reason("explicit_side_equal", *[_fields(row, "lesion.laterality") for row in rows]))
    else:
        blockers.add("side_unknown_or_ambiguous")
    if bodies[0] and bodies[0] == bodies[1] and len(bodies[0]) == 1:
        reasons.append(_reason("explicit_body_equal", *[_fields(row, "imaging.body_site") for row in rows]))
    else:
        blockers.add("body_unknown_or_ambiguous")
    methods = [_values(row, "imaging.modality", "code") - {"UNKNOWN"} for row in rows]
    if not all(methods):
        blockers.add("method_unknown")
    elif methods[0] != methods[1]:
        blockers.add("method_changed")
    evidence = [field for row in rows for key in ("lesion.site", "lesion.laterality", "report.exam_date",
        "imaging.body_site", "imaging.modality", "comparison.statement", "comparison.reference_date")
        for field in _fields(row, key)]
    if any(not row.get("source_usable") for row in rows) or any(not field["usable"] for field in evidence):
        blockers.add("unconfirmed_source")
    if any(field.get("conflict") for field in evidence):
        blockers.add("field_conflict")
    if not all(_exam_day(row) for row in rows):
        blockers.add("date_unreliable")
    elif _exam_day(first) == _exam_day(second):
        blockers.add("same_exam_day")
    for reference_row, exam_row in (rows, rows[::-1]):
        day = _exam_day(exam_row)
        references = [field for field in _fields(reference_row, "comparison.reference_date")
                      if day and _day(field) == day]
        if references:
            reasons.append(_reason("reference_date_equal", references, _fields(exam_row, "report.exam_date")))
    if any(marker in str(field["content"]["value"].get("text", "")) for row in rows
           for field in _fields(row, "comparison.statement") for marker in UNCERTAIN):
        blockers.add("uncertain_comparison")
    return first, second, reasons, blockers


def propose_matches(observations):
    pairs = [pair for first, second in combinations(sorted(observations, key=lambda row: row["id"]), 2)
             if (pair := _pair(first, second)) is not None]
    counts = Counter((endpoint["id"], other["report_id"]) for first, second, _, _ in pairs
                     for endpoint, other in ((first, second), (second, first)))
    result = []
    for first, second, reasons, blockers in pairs:
        if counts[(first["id"], second["report_id"])] > 1 or counts[(second["id"], first["report_id"])] > 1:
            blockers.add("multiple_candidates")
        reasons = tuple(sorted(reasons, key=lambda reason: (reason["code"], reason["field_ids"])))
        blockers = tuple(sorted(blockers))
        first_binding, second_binding = deepcopy(first["source_binding"]), deepcopy(second["source_binding"])
        # A newly discovered alternative changes the displayed ambiguity, not
        # the identity of this same source pair or its previous rejection.
        identity = {"rule": RULE_VERSION, "first": [first["id"], first_binding],
                    "second": [second["id"], second_binding]}
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()
        result.append(MatchProposal(first["id"], second["id"], first_binding, second_binding,
                                    reasons, blockers, fingerprint))
    return sorted(result, key=lambda proposal: (proposal.first_id, proposal.second_id))
