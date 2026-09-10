"""Pure scoring against independent original OCR and a frozen scoped field gold.

The caller verifies original file/OCR hashes before making the plain ``originals``
index. Neither runtime UUIDs nor predicted values select a gold pairing. No ORM,
parser, network or filesystem operation occurs here. Real execution requires a
separate, exact harness/source/gold/protocol approval.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
import unicodedata


VERSION = "PATHOLOGY_IHC_SCOPED_SCORER_V1"
STATES = ("CORRECT", "MISMATCH", "SOURCE_UNVERIFIED", "MISSING")
COMPONENTS = ("value", "raw_unit_and_unit_state", "score_kind_and_scale", "original_date_role_and_precision",
              "source_role_and_assertion", "specimen_assay_marker_binding", "own_original_source_proof")
SOURCE_KEYS = ("source_sha256", "ocr_sha256", "page")
SCOPED = "SCOPED_FIELDS_AND_EXCLUSIONS"
SCOPE_STATES = {SCOPED, "SCOPE_VISUAL_ONLY_NOT_IHC_GOLD", "UNREVIEWED_UNJUDGED"}
MISSING = object()


class EvaluationInputError(ValueError):
    """The frozen inputs are inconsistent; no replacement denominator is valid."""


def _page_key(row):
    if not isinstance(row, dict):
        return None
    if any(not isinstance(row.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", row[key]) for key in SOURCE_KEYS[:2]):
        return None
    if type(row.get("page")) is not int or row["page"] < 1:
        return None
    return tuple(row[key] for key in SOURCE_KEYS)


def _polygon(value):
    return (isinstance(value, list) and len(value) >= 3 and all(
        isinstance(point, (list, tuple)) and len(point) == 2 and all(
            type(number) in {int, float} and math.isfinite(number) and 0 <= number <= 1 for number in point)
        for point in value))


def _compact(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value) if not c.isspace())


def _equal(expected, actual, *, decimal=False):
    if actual is MISSING:
        return False
    if decimal:
        if not isinstance(expected, str) or not isinstance(actual, str):
            return False
        try:
            a, b = Decimal(_compact(expected)), Decimal(_compact(actual))
            return a.is_finite() and b.is_finite() and a == b
        except (InvalidOperation, ValueError):
            return False
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(expected) == set(actual) and all(_equal(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(_equal(a, b) for a, b in zip(expected, actual))
    if isinstance(expected, str):
        return isinstance(actual, str) and _compact(expected) == _compact(actual)
    return type(expected) is type(actual) and expected == actual


class OriginalIndex:
    def __init__(self, originals):
        self.pages = {}
        if not isinstance(originals, dict) or not isinstance(originals.get("pages"), list):
            raise EvaluationInputError("Original page inventory is required")
        for page in originals["pages"]:
            key = _page_key(page)
            if key is None or key in self.pages or not isinstance(page.get("regions"), list):
                raise EvaluationInputError("Original pages must have unique complete identities")
            orders = set()
            for region in page["regions"]:
                if (not isinstance(region, dict) or not isinstance(region.get("text"), str)
                        or type(region.get("reading_order")) is not int or region["reading_order"] < 0
                        or region["reading_order"] in orders or not _polygon(region.get("polygon"))):
                    raise EvaluationInputError("Original regions need unique order, text and original polygons")
                orders.add(region["reading_order"])
            self.pages[key] = page

    def proof_valid(self, proof):
        key = _page_key(proof)
        if key not in self.pages:
            return False
        index = proof.get("region_index")
        regions = self.pages[key]["regions"]
        if type(index) is not int or not 0 <= index < len(regions):
            return False
        region = regions[index]
        start, end = proof.get("start_offset"), proof.get("end_offset")
        if (type(proof.get("reading_order")) is not int or proof["reading_order"] != region["reading_order"]
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(region["text"])
                or not _polygon(proof.get("polygon")) or proof["polygon"] != region["polygon"]
                or proof.get("raw_text") != region["text"][start:end]):
            return False
        aliases = {"region_array_index": index, "block_reading_order": region["reading_order"], "manifest_page": key[2],
                   "char_start": start, "char_end_exclusive": end, "fixed_ocr_sha256": key[1],
                   "original_normalized_polygon": region["polygon"], "block_text": region["text"],
                   "full_block_text_sha256": hashlib.sha256(region["text"].encode("utf8")).hexdigest()}
        return all(name not in proof or (type(proof[name]) is type(value) and proof[name] == value) for name, value in aliases.items())

    def proves(self, supplied, required):
        if not isinstance(supplied, list) or not supplied or not all(self.proof_valid(p) for p in supplied):
            return False
        for wanted in required:
            if not any(_covers(proof, wanted, self.pages) for proof in supplied):
                return False
        return True


def _covers(proof, wanted, pages):
    if (_page_key(proof) != _page_key(wanted) or proof["region_index"] != wanted["region_index"]
            or not proof["start_offset"] <= wanted["start_offset"] < wanted["end_offset"] <= proof["end_offset"]):
        return False
    if (proof["start_offset"], proof["end_offset"]) == (wanted["start_offset"], wanted["end_offset"]):
        return True
    text = pages[_page_key(proof)]["regions"][proof["region_index"]]["text"]
    return text[proof["start_offset"]:proof["end_offset"]].count(wanted["raw_text"]) == 1


def _family(key):
    if isinstance(key, str) and key.startswith("assay.") and key.endswith("_date"):
        return "assay.DATE"
    return key


def _located_at(item, target, envelope):
    """Recall by physical slot only; contradictions are assessed as proof faults."""
    if _family(item.get("field_key")) != _family(target["field_key"]) or envelope != _page_key(target):
        return False
    for supplied in item.get("value_evidence", []) if isinstance(item.get("value_evidence"), list) else []:
        if not isinstance(supplied, dict):
            continue
        for wanted in target["value_evidence"]:
            if (supplied.get("page") == wanted["page"] and supplied.get("region_index") == wanted["region_index"]
                    and type(supplied.get("start_offset")) is int and type(supplied.get("end_offset")) is int
                    and max(supplied["start_offset"], wanted["start_offset"]) < min(supplied["end_offset"], wanted["end_offset"])):
                return True
    return False


def _applicable(target):
    value, key = target["expected_value"], target["field_key"]
    return {"value": True, "raw_unit_and_unit_state": "unit" in value or any("unit" in c for c in value.get("components", [])),
            "score_kind_and_scale": key == "ihc.score", "original_date_role_and_precision": _family(key) == "assay.DATE",
            "source_role_and_assertion": True, "specimen_assay_marker_binding": bool(target.get("bindings")),
            "own_original_source_proof": True}


def _semantic_components(target, item):
    expected = target["expected_value"]
    actual = item.get("value") if isinstance(item.get("value"), dict) else {}
    applicable = _applicable(target)
    base_keys = set(expected) - {"unit", "unit_state", "score_kind", "scale_kind", "assertion", "precision"}
    if target["field_key"] in {"ihc.score", "specimen.dimensions", "specimen.nodes"}:
        base_keys.discard("raw")
    value_ok = True
    for key in base_keys:
        a, b = expected[key], actual.get(key, MISSING)
        if key == "values":
            okay = isinstance(b, list) and len(a) == len(b) and all(_equal(x, y, decimal=True) for x, y in zip(a, b))
        elif key == "components":
            okay = isinstance(b, list) and len(a) == len(b) and all(isinstance(y, dict)
                and _equal(x.get("value"), y.get("value", MISSING), decimal=True)
                and _equal(x.get("axis"), y.get("axis", MISSING)) for x, y in zip(a, b))
        elif key == "groups":
            cleaned = lambda groups: [{k: v for k, v in group.items() if k != "raw"} for group in groups]
            okay = isinstance(b, list) and all(isinstance(g, dict) for g in b) and _equal(cleaned(a), cleaned(b))
        else:
            okay = _equal(a, b)
        value_ok = value_ok and okay
    unit_ok = all(_equal(expected[key], actual.get(key, MISSING)) for key in ("unit", "unit_state") if key in expected)
    if "components" in expected and applicable["raw_unit_and_unit_state"]:
        units = actual.get("components")
        unit_ok = unit_ok and isinstance(units, list) and len(units) == len(expected["components"]) and all(
            isinstance(b, dict) and _equal(a.get("unit", MISSING), b.get("unit", MISSING))
            for a, b in zip(expected["components"], units))
    checks = {
        "value": value_ok,
        "raw_unit_and_unit_state": unit_ok,
        "score_kind_and_scale": all(_equal(expected[key], actual.get(key, MISSING)) for key in ("score_kind", "scale_kind") if key in expected),
        "original_date_role_and_precision": item.get("field_key") == target["field_key"] and _equal(expected.get("precision"), actual.get("precision", MISSING)),
        "source_role_and_assertion": _equal(target["source_role"], item.get("source_role", MISSING))
            and ("assertion" not in expected or _equal(expected["assertion"], actual.get("assertion", MISSING))),
    }
    return {name: ("CORRECT" if okay else "MISMATCH") for name, okay in checks.items() if applicable[name]}


def _own_proof(target, item, originals):
    diagnostics = item.get("mapping_diagnostics", [])
    if not isinstance(diagnostics, list) or any(not isinstance(d, dict) or d.get("severity", "ERROR") != "INFO" for d in diagnostics):
        return False
    return (originals.proves(item.get("value_evidence"), target["value_evidence"])
            and originals.proves(item.get("label_evidence"), target["label_evidence"]))


def _aggregate(states):
    for state in ("MISMATCH", "SOURCE_UNVERIFIED", "MISSING"):
        if state in states:
            return state
    return "CORRECT"


def _binding_status(target, item, by_id, originals, depth=0):
    if depth > 8:
        return "SOURCE_UNVERIFIED"
    actual = item.get("bindings")
    if not isinstance(actual, dict):
        return "SOURCE_UNVERIFIED"
    states = []
    for role, identity in target.get("bindings", {}).items():
        expected = by_id[identity]
        binding = actual.get(role)
        if not isinstance(binding, dict) or binding.get("state") != "BOUND" or not isinstance(binding.get("target"), dict):
            states.append("SOURCE_UNVERIFIED")
            continue
        node = binding["target"]
        if node.get("field_key") != expected["field_key"]:
            states.append("MISMATCH")
            continue
        proof = node.get("value_evidence")
        if isinstance(proof, list) and proof and all(originals.proof_valid(p) for p in proof):
            if not originals.proves(proof, expected["value_evidence"]):
                states.append("MISMATCH")
                continue
        if not _own_proof(expected, node, originals) or not originals.proves(binding.get("proof_evidence"), expected["value_evidence"]):
            states.append("SOURCE_UNVERIFIED")
        states.extend(_semantic_components(expected, node).values())
        if expected.get("bindings"):
            states.append(_binding_status(expected, node, by_id, originals, depth + 1))
    if set(actual) != set(target.get("bindings", {})):
        states.append("MISMATCH")
    return _aggregate(states)


def _validate_gold(gold, originals, predicates):
    if not isinstance(gold, dict) or not all(isinstance(gold.get(k), list) for k in ("fields", "coverage", "negative_boundaries")):
        raise EvaluationInputError("Fields, coverage and exclusion regions must be explicit")
    coverage = {}
    for page in gold["coverage"]:
        key = _page_key(page)
        if key is None or key not in originals.pages or key in coverage or page.get("state") not in SCOPE_STATES:
            raise EvaluationInputError("Gold coverage must agree with the independent original inventory")
        coverage[key] = page
    if set(coverage) != set(originals.pages):
        raise EvaluationInputError("The complete original page denominator must be represented in coverage")
    by_id = {}
    for field in gold["fields"]:
        identity = field.get("gold_id")
        if (not isinstance(identity, str) or not identity.strip() or identity in by_id or _page_key(field) not in coverage
                or not isinstance(field.get("field_key"), str) or not isinstance(field.get("expected_value"), dict)
                or not isinstance(field.get("source_role"), str) or not isinstance(field.get("bindings"), dict)):
            raise EvaluationInputError("Gold fields require unique IDs and complete contracts")
        if coverage[_page_key(field)]["state"] != SCOPED:
            raise EvaluationInputError("A field cannot turn an unjudged page into a gold target")
        for key in ("value_evidence", "label_evidence"):
            if not isinstance(field.get(key), list) or not field[key] or not all(originals.proof_valid(p) for p in field[key]):
                raise EvaluationInputError("Gold source evidence disagrees with original OCR")
        if _page_key(field["value_evidence"][0]) != _page_key(field):
            raise EvaluationInputError("Gold primary page disagrees with value evidence")
        by_id[identity] = field

    def visit(identity, stack):
        if identity not in by_id or identity in stack or len(stack) > 8:
            raise EvaluationInputError("Gold bindings are missing, cyclic or exceed the permitted depth")
        for other in by_id[identity]["bindings"].values():
            visit(other, (*stack, identity))
    for identity in by_id:
        visit(identity, ())
    rules = {}
    if not isinstance(predicates, dict) or not isinstance(predicates.get("rules"), list):
        raise EvaluationInputError("Explicit reviewed negative predicates are required")
    for row in predicates["rules"]:
        identity, predicate = row.get("negative_id"), row.get("predicate")
        if (not isinstance(identity, str) or not identity or identity in rules or not isinstance(predicate, dict)
                or not isinstance(predicate.get("field_keys"), list) or not predicate["field_keys"]
                or predicate.get("condition") not in {"ANY", "NON_NULL_DATE", "NON_EMPTY_ASSERTED_TEXT"}):
            raise EvaluationInputError("Negative predicate is missing, repeated or unsupported")
        rules[identity] = predicate
    negative_ids = set()
    for boundary in gold["negative_boundaries"]:
        identity = boundary.get("negative_id")
        if identity not in rules or identity in negative_ids or _page_key(boundary) not in coverage:
            raise EvaluationInputError("Every original exclusion region needs exactly one predicate")
        negative_ids.add(identity)
        if not boundary.get("evidence") or not all(originals.proof_valid(p) for p in boundary["evidence"]):
            raise EvaluationInputError("Negative original evidence is inconsistent")
    if negative_ids != set(rules):
        raise EvaluationInputError("A predicate must not add an unannotated exclusion region")
    denominators = gold.get("denominators", {})
    expected_counts = {"target_fields": len(by_id), "score_fields": sum(f["field_key"] == "ihc.score" for f in by_id.values()),
                       "identity_and_metadata_fields": sum(f["field_key"] != "ihc.score" for f in by_id.values()),
                       "negative_regions": len(negative_ids), "gold_scoped_pages": sum(p["state"] == SCOPED for p in coverage.values()),
                       "other_scope_reviewed_pages_not_field_gold": sum(p["state"] == "SCOPE_VISUAL_ONLY_NOT_IHC_GOLD" for p in coverage.values()),
                       "unreviewed_pages": sum(p["state"] == "UNREVIEWED_UNJUDGED" for p in coverage.values())}
    if not isinstance(denominators, dict) or any(k in denominators and (type(denominators[k]) is not int or denominators[k] != value)
                                                 for k, value in expected_counts.items()):
        raise EvaluationInputError("Explicit frozen denominators disagree with the annotated scope")
    return coverage, by_id, rules


def _condition(predicate, item, predicates):
    if item.get("field_key") not in predicate["field_keys"]:
        return False
    value = item.get("value") if isinstance(item.get("value"), dict) else {}
    if predicate["condition"] == "ANY":
        return True
    if predicate["condition"] == "NON_NULL_DATE":
        return "value" in value and value["value"] is not None and value["value"] != ""
    text = value.get("text")
    unknown = {_compact(v) for v in predicates.get("unknown_text_literals", []) if isinstance(v, str)}
    return isinstance(text, str) and _compact(text) not in unknown and value.get("assertion") != "NOT_PROVIDED"


def _boundary_at(item, boundary, originals):
    proofs = item.get("value_evidence", [])
    if not isinstance(proofs, list):
        return False
    return any(originals.proof_valid(p) and _page_key(p) == _page_key(w) and p["region_index"] == w["region_index"]
               and max(p["start_offset"], w["start_offset"]) < min(p["end_offset"], w["end_offset"])
               for p in proofs for w in boundary["evidence"])


def evaluate(gold, predictions, originals, negative_predicates):
    index = OriginalIndex(originals)
    coverage, by_id, rules = _validate_gold(gold, index, negative_predicates)
    if not isinstance(predictions, dict) or not isinstance(predictions.get("pages"), list):
        raise EvaluationInputError("Prediction page execution receipts are required")
    pages, invalid = {}, set()
    diagnostics, candidates = [], []
    invalid_envelopes = 0
    for page in predictions["pages"]:
        key = _page_key(page)
        if key not in coverage:
            invalid_envelopes += 1
            continue
        if key in pages:
            invalid.add(key)
        pages.setdefault(key, []).append(page)
        if page.get("status") not in {"COMPLETE", "FAILED", "NOT_RUN"} or not isinstance(page.get("items"), list):
            invalid.add(key)
    statuses = {}
    for key in coverage:
        statuses[key] = "INVALID" if key in invalid else pages[key][0]["status"] if key in pages else "NOT_RUN"
    # Keep every original input ordinal, even when the surrounding execution
    # envelope is unusable. An invalid page must not erase its candidate count.
    for envelope in predictions["pages"]:
        key = _page_key(envelope)
        items = envelope.get("items", []) if isinstance(envelope, dict) else []
        for item in items if isinstance(items, list) else []:
            ordinal = len(candidates)
            item = item if isinstance(item, dict) else {}
            values = item.get("value_evidence")
            wrong_primary = bool(key and isinstance(values, list) and values and isinstance(values[0], dict)
                                 and values[0].get("page") != key[2])
            candidates.append({"ordinal": ordinal, "item": item, "page": key,
                               "eligible": key in coverage and statuses[key] == "COMPLETE" and not wrong_primary,
                               "invalid_envelope": key not in coverage, "invalid_primary": wrong_primary})
    used, assignments = set(), []
    sort_key = lambda field: (_page_key(field), field["value_evidence"][0]["region_index"], field["value_evidence"][0]["start_offset"], field["gold_id"])
    for target in sorted(gold["fields"], key=sort_key):
        matches = [row for row in candidates if row["eligible"] and row["ordinal"] not in used and _located_at(row["item"], target, row["page"])]
        chosen = matches[0] if matches else None
        applies = _applicable(target)
        states = {name: "MISSING" for name, yes in applies.items() if yes}
        if chosen:
            used.add(chosen["ordinal"])
            states.update(_semantic_components(target, chosen["item"]))
            states["own_original_source_proof"] = "CORRECT" if _own_proof(target, chosen["item"], index) else "SOURCE_UNVERIFIED"
            if applies["specimen_assay_marker_binding"]:
                states["specimen_assay_marker_binding"] = _binding_status(target, chosen["item"], by_id, index)
        assignments.append({"gold_id": target["gold_id"], "field_key": target["field_key"], "status": _aggregate(states.values()),
                            "candidate_ordinal": chosen["ordinal"] if chosen else None, "page_execution": statuses[_page_key(target)],
                            "components": {name: {"applicable": applies[name], "status": states.get(name, "NOT_APPLICABLE")} for name in COMPONENTS}})
    policy = negative_predicates.get("role_policy", {})
    for row in candidates:
        item, ordinal = row["item"], row["ordinal"]
        classification, boundary_id = "MATCHED", None
        if not row["eligible"]:
            classification = "INVALID_PAGE_ENVELOPE" if row["invalid_envelope"] else "INVALID_ITEM_ENVELOPE" if row["invalid_primary"] else "UNTRUSTED_EXECUTION"
        elif ordinal not in used:
            if any(_located_at(item, target, row["page"]) for target in gold["fields"]):
                classification = "DUPLICATE"
            else:
                boundaries = [b for b in gold["negative_boundaries"] if _page_key(b) == row["page"] and _boundary_at(item, b, index)]
                if boundaries:
                    # Several annotated restrictions may share original text.
                    # Evaluate their explicit predicates, not only the first.
                    applicable = [b for b in boundaries if _condition(rules[b["negative_id"]], item, negative_predicates)]
                    boundary = applicable[0] if applicable else boundaries[0]
                    boundary_id = boundary["negative_id"]
                    role = item.get("source_role")
                    if role in policy.get("attributed_exclusion", []):
                        classification = "EXCLUDED_ROLE"
                    elif role not in policy.get("patient_admission", []):
                        classification = "UNJUDGED_ROLE"
                    else:
                        classification = "FALSE_ADMISSION" if _condition(rules[boundary_id], item, negative_predicates) else "OUT_OF_GOLD_UNJUDGED"
                else:
                    proofs = item.get("value_evidence")
                    classification = "OUT_OF_GOLD_UNJUDGED" if isinstance(proofs, list) and proofs and all(index.proof_valid(p) for p in proofs) else "UNLOCATED"
        diagnostics.append({"candidate_ordinal": ordinal, "classification": classification, "negative_id": boundary_id})
    def outcomes(rows):
        return {"denominator": len(rows), **{state: sum(row["status"] == state for row in rows) for state in STATES}}
    count = Counter(row["classification"] for row in diagnostics)
    summary = {
        "field_outcomes": {state: sum(row["status"] == state for row in assignments) for state in STATES},
        "score_fields": outcomes([r for r in assignments if r["field_key"] == "ihc.score"]),
        "context_fields": outcomes([r for r in assignments if r["field_key"] != "ihc.score"]),
        "components": {name: outcomes([r["components"][name] for r in assignments if r["components"][name]["applicable"]]) for name in COMPONENTS},
        "execution": {name.lower() + "_pages": sum(value == name for value in statuses.values()) for name in ("COMPLETE", "FAILED", "NOT_RUN", "INVALID")},
        "scope": {"pages": len(coverage), "unreviewed_pages": sum(p["state"] == "UNREVIEWED_UNJUDGED" for p in coverage.values()),
                  "out_of_gold_pages": sum(p["state"] != SCOPED for p in coverage.values())},
        "negative_regions": {"denominator": len(gold["negative_boundaries"]), "executed": sum(statuses[_page_key(b)] == "COMPLETE" for b in gold["negative_boundaries"])},
        "duplicate_candidates": count["DUPLICATE"], "false_admissions": count["FALSE_ADMISSION"],
        "unlocated_candidates": count["UNLOCATED"], "out_of_gold_candidates": count["OUT_OF_GOLD_UNJUDGED"],
        "candidate_classifications": dict(count), "candidate_count": len(candidates),
        "invalid_page_envelopes": invalid_envelopes,
        "complete_report_accuracy": None, "clinical_accuracy": None,
    }
    return {"scorer_version": VERSION, "summary": summary, "assignments": assignments, "candidate_diagnostics": diagnostics}
