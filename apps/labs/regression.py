"""A bounded, public SYNTHETIC publication gate; no patient data or database I/O.

Only the two frozen LF/CRLF v1 byte identities may use the limited legacy scope. Every other
dictionary executes the complete Phase Two corpus, including missing targets.
Expected identities, values, units and context are read solely from the frozen
fixture. A baseline dictionary is evaluated with the same parser and corpus;
field recall is compared only on identical, assessed case/field IDs.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import unicodedata

from apps.processing.value_objects import OcrPage, OcrRegion

from . import extraction
from .dictionary import IndicatorDictionary


_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_PATH = Path(__file__).resolve().parent / "dictionaries/phase-two-regression.json"
_PARSER_FILES = (
    "apps/labs/candidates.py",
    "apps/labs/dictionary.py",
    "apps/labs/extraction.py",
    "apps/labs/layout.py",
    "apps/labs/models.py",
    "apps/labs/numerics.py",
    "apps/labs/quality.py",
    "apps/labs/regression.py",
    "apps/labs/validation.py",
    "apps/processing/value_objects.py",
    "tools/sample_dictionary/normalize.py",
)
_FIELDS = (
    "standard_code", "raw_name", "raw_value", "raw_unit", "result_type",
    "specimen", "reference_range_raw", "report_flag_raw", "page_number",
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _definition_digest(dictionary):
    # A caller cannot claim legacy scope by copying the old content_hash onto
    # an edited in-memory dictionary.
    return _digest({
        "version": dictionary.version,
        "generated_from": dictionary.generated_from,
        "indicators": [asdict(item) for item in dictionary.indicators],
    })


def _load_corpus():
    encoded = _CORPUS_PATH.read_bytes()
    corpus = json.loads(encoded)
    targets = corpus["targets"]
    normal = corpus["normal_controls"]
    layout_controls = [item for case in corpus["layout_cases"] for item in case["expected"] if item.get("must_block") is False]
    if (
        corpus["schema_version"] != "1.0"
        or corpus["dataset_kind"] != "SYNTHETIC"
        or len(targets) != 125
        or len({target["code"] for target in targets}) != 125
        or sum(len(target["results"]) for target in targets) != 373
        or any(len({result[1] for result in target["results"]}) != len(target["results"]) for target in targets)
        or {result[1] for target in targets for result in target["results"]} != {"numeric", "comparator", "qualitative", "semi_quantitative", "status"}
        or sum(target["legacy_name"] is not None for target in targets) != 55
        or sum(len(target["results"]) for target in targets if target["legacy_name"] is not None) != 160
        or len(corpus["genes"]) != 50
        or len({gene["code"] for gene in corpus["genes"]}) != 50
        or {target["code"] for target in targets} & {gene["code"] for gene in corpus["genes"]}
        or sum(map(len, corpus["alias_contexts"].values())) != 23
        or len(corpus["layout_cases"]) != 17
        or len(set(normal["target_codes"])) != 43
        or not set(normal["target_codes"]) <= {target["code"] for target in targets}
        or sum(result[1] in {"numeric", "comparator"} for target in targets if target["code"] in normal["target_codes"] for result in target["results"]) != 86
        or normal["result_types"] != ["numeric", "comparator"]
        or normal["capability_level"] != "STABLE"
        or normal["quality_codes"] != []
        or normal.get("minimum_confidence") != .95
        or normal["expected_observations"] != 98
        or normal["layout_observations"] != 12
        or len(layout_controls) != 12
        or any(item.get("capability_level") != "STABLE" or item.get("quality_codes") != [] or not item["specimen"] for item in layout_controls)
        or any(item.get("minimum_confidence") != .95 for item in layout_controls)
    ):
        raise ValueError("fixed_synthetic_corpus_invalid")
    return corpus, hashlib.sha256(encoded).hexdigest()


def _page(specification):
    regions = []
    for y, cells in specification["rows"]:
        for cell in cells:
            x, text, *geometry = cell
            width, height, confidence = geometry or (.07, .025, .99)
            polygon = ((x, y), (x + width, y), (x + width, y + height), (x, y + height))
            regions.append(OcrRegion(text, polygon, confidence, len(regions)))
    return OcrPage(specification["page_number"], 1000, 1400, tuple(regions), "SYNTHETIC", "1")


def _target_table(corpus, target, result, *, legacy):
    template = corpus["table_template"]
    value, result_type, unit = result
    raw_name = target["legacy_name"] if legacy else target["positive"]["raw_name"]
    context = [] if legacy else target["context_text"]
    rows = [
        [template["context_y"] + index * template["context_step"], [[template["context_x"], text]]]
        for index, text in enumerate(context)
    ]
    cells = [[template["name_x"], raw_name], [template["value_x"], value]]
    if unit:
        cells.append([template["unit_x"], unit])
    rows.extend([template["headers"], [template["row_y"], cells]])
    source_bounds = {
        name: [template[anchor], template["row_y"], template[anchor] + .07, template["row_y"] + .025]
        for name, anchor in (("raw_name", "name_x"), ("raw_value", "value_x"), ("raw_unit", "unit_x"))
        if name != "raw_unit" or unit
    }
    case = {
        "pages": [{"page_number": 1, "rows": rows}],
        "expected": [{
            "standard_code": target["code"], "raw_name": raw_name,
            "raw_value": value, "raw_unit": unit, "result_type": result_type,
            "specimen": "" if legacy else target["positive"]["specimen"],
            "reference_range_raw": "", "report_flag_raw": "", "page_number": 1,
            "source_bounds": source_bounds,
        }],
    }
    normal = corpus["normal_controls"]
    if not legacy and target["code"] in normal["target_codes"] and result_type in normal["result_types"]:
        case["expected"][0].update({
            "must_block": False, "capability_level": normal["capability_level"],
            "quality_codes": normal["quality_codes"],
            "minimum_confidence": normal["minimum_confidence"],
        })
    return case


def _bucket(total):
    return {"total": total, "assessed": 0, "passed": 0, "failed": 0, "unassessed": total}


def _evaluate(dictionary, corpus, *, observer=None):
    legacy = (
        # Preserve the loader's raw-byte identity: only these two historical
        # checkout representations qualify, never arbitrary JSON normalization.
        dictionary.content_hash in corpus["legacy_identity"]["dictionary_sha256_by_line_ending"].values()
        and _definition_digest(dictionary) == corpus["legacy_identity"]["definition_sha256"]
    )
    targets, genes = corpus["targets"], corpus["genes"]
    codes = {item.code for item in dictionary.indicators}
    missing = sorted(target["code"] for target in targets if target["code"] not in codes)
    severe_total = sum(any(item.get("must_block") for item in case["expected"]) for case in corpus["layout_cases"])
    counts = {
        "target_positive": _bucket(125), "target_negative": _bucket(125),
        "alias_context": _bucket(23), "gene_positive": _bucket(50),
        "legacy_positive": _bucket(55), "target_parser": _bucket(373),
        "legacy_parser": _bucket(160), "layout_parser": _bucket(len(corpus["layout_cases"])),
        "severe": {"total": severe_total, "assessed": 0, "blocked": 0, "escaped": 0, "unassessed": severe_total},
        "normal": {
            "scope": corpus["normal_controls"]["scope"], "total": 98,
            "minimum_confidence": corpus["normal_controls"]["minimum_confidence"],
            "assessed": 0, "preserved": 0, "routed": 0, "missing": 0, "unassessed": 98,
        },
        "observations": {"expected": 0, "actual": 0, "missing": 0, "unexpected": 0},
    }
    failures, checks, result_types = [], {}, {}

    def failure(case_id, reason, **details):
        failures.append({"case_id": case_id, "reason": reason, **details})

    def assessed(bucket, before):
        bucket["assessed"] += 1
        bucket["unassessed"] -= 1
        bucket["passed" if len(failures) == before else "failed"] += 1

    def match_case(bucket, case_id, raw_name, specimen, panel, expected):
        before = len(failures)
        try:
            match = dictionary.match(raw_name, specimen=specimen, panel=panel)
            actual = match.code if match else None
            if actual != expected:
                failure(case_id, "dictionary_mapping_mismatch", expected_code=expected, actual_code=actual)
        except Exception as error:
            # Never persist candidate-controlled exception text or source data.
            failure(case_id, "dictionary_mapping_error", error_type=type(error).__name__)
        assessed(counts[bucket], before)

    def field(case_id, index, name, expected, actual, *, absent=False, reason=None):
        correct = not absent and expected == actual
        checks[(case_id, index, name)] = {"correct": correct, "absent": absent}
        if not correct:
            # Only fixed field names, codes and counts are exposed in failures.
            failure(case_id, reason or ("observation_missing" if absent else "field_mismatch"), observation=index, field=name)
        return correct

    def parser_case(bucket, case_id, case):
        before = len(failures)
        error_type = None
        try:
            pages = tuple(_page(page) for page in case["pages"])
            actual = extraction.extract_observations(pages, dictionary)
        except Exception as error:
            actual = ()
            error_type = type(error).__name__
            failure(case_id, "parser_error", error_type=type(error).__name__)
        if observer is not None:
            observer(case_id, case, actual, error_type)
        expected = case["expected"]
        counts["observations"]["expected"] += len(expected)
        counts["observations"]["actual"] += len(actual)
        counts["observations"]["missing"] += max(0, len(expected) - len(actual))
        counts["observations"]["unexpected"] += max(0, len(actual) - len(expected))
        if len(actual) > len(expected):
            failure(case_id, "unexpected_observations", expected_count=len(expected), actual_count=len(actual))
        blocked = []
        for index, want in enumerate(expected):
            item = actual[index] if index < len(actual) else None
            kind_counts = result_types.setdefault(want["result_type"], {"expected": 0, "correct": 0})
            kind_counts["expected"] += 1
            kind_counts["correct"] += bool(item is not None and item.result_type.lower() == want["result_type"])
            for name in _FIELDS:
                actual_value = getattr(item, name, None)
                if name == "result_type" and actual_value is not None:
                    actual_value = actual_value.lower()
                field(case_id, index, name, want[name], actual_value, absent=item is None)
            # A field must remain locatable on the observation's source page.
            # This corpus has exact OCR cells; page-only fallback cannot count
            # as an exact location for the ordinary table rows.
            if item is not None:
                source_fields = [name for name in ("raw_name", "raw_value", "raw_unit") if want[name]]
                located = all(
                    item.field_evidence.get(name, {}).get("page_number") == want["page_number"]
                    and item.field_evidence.get(name, {}).get("precision") == "region"
                    and bool(item.field_evidence.get(name, {}).get("polygon"))
                    for name in source_fields
                )
                for name, bounds in want.get("source_bounds", {}).items():
                    polygon = item.field_evidence.get(name, {}).get("polygon") or ()
                    actual_bounds = (
                        [min(point[0] for point in polygon), min(point[1] for point in polygon),
                         max(point[0] for point in polygon), max(point[1] for point in polygon)]
                        if polygon else ()
                    )
                    located = located and len(actual_bounds) == 4 and all(abs(got - expected) < 1e-9 for got, expected in zip(actual_bounds, bounds))
            else:
                located = False
            field(case_id, index, "source_location", True, located, absent=item is None)
            if want.get("must_block"):
                issues = {entry["code"] for entry in item.quality_issues} if item is not None else set()
                protected = (
                    item is not None and item.capability_level == "SEARCH_ONLY"
                    and set(want["quality_codes"]) <= issues
                )
                blocked.append(protected)
                field(case_id, index, "severe_error_blocked", True, protected, absent=item is None)
            elif want.get("must_block") is False:
                # These controls were frozen with reviewed units and explicit
                # specimen. Never select the denominator from candidate output.
                issues = sorted(entry["code"] for entry in item.quality_issues) if item is not None else None
                preserved = (
                    item is not None and item.capability_level == want["capability_level"]
                    and issues == sorted(want["quality_codes"])
                    and item.confidence >= want["minimum_confidence"]
                )
                normal = counts["normal"]
                normal["assessed"] += 1
                normal["unassessed"] -= 1
                normal["preserved"] += preserved
                normal["routed"] += item is not None and not preserved
                normal["missing"] += item is None
                field(case_id, index, "normal_control_preserved", True, preserved,
                      absent=item is None, reason="normal_control_degraded")
            for name, raw, normalized in want.get("normalization", []):
                candidates = item.normalization_candidates if item is not None else ()
                preserved = any(candidate["field"] == name and candidate["before"] == raw and candidate["after"] == normalized for candidate in candidates)
                field(case_id, index, "normalization_candidate", True, preserved, absent=item is None)
            if want.get("source_fragments"):
                preserved = item is not None and all(text in item.source_text for text in want["source_fragments"])
                field(case_id, index, "source_preserved", True, preserved, absent=item is None)
        if blocked:
            severe = counts["severe"]
            severe["assessed"] += 1
            severe["unassessed"] -= 1
            severe["blocked" if all(blocked) else "escaped"] += 1
        assessed(counts[bucket], before)

    if not legacy:
        for code in missing:
            failure("coverage:" + code, "missing_target", expected_code=code)
        for target in targets:
            positive, negative = target["positive"], target["negative"]
            match_case("target_positive", "positive:" + target["code"], **positive, expected=target["code"])
            match_case("target_negative", "negative:" + target["code"], negative["raw_name"], negative["specimen"], negative["panel"], negative["expected_code"])
            for value in target["results"]:
                parser_case("target_parser", "target:" + target["code"] + ":" + value[1], _target_table(corpus, target, value, legacy=False))
        for group, cases in corpus["alias_contexts"].items():
            for index, case in enumerate(cases):
                match_case("alias_context", f"context:{group}:{index}", *case)
        for case in corpus["layout_cases"]:
            parser_case("layout_parser", "layout:" + case["id"], case)

    for target in targets:
        if target["legacy_name"] is None:
            continue
        match_case("legacy_positive", "legacy-positive:" + target["code"], target["legacy_name"], "", "", target["code"])
        for value in target["results"]:
            parser_case("legacy_parser", "legacy:" + target["code"] + ":" + value[1], _target_table(corpus, target, value, legacy=True))
    for gene in genes:
        match_case("gene_positive", "gene:" + gene["code"], gene["raw_name"], "" if legacy else gene["specimen"], "" if legacy else gene["panel"], gene["code"])

    fields = {}
    for (_, _, name), result in checks.items():
        summary = fields.setdefault(name, {"expected": 0, "correct": 0, "missed": 0, "wrong": 0})
        summary["expected"] += 1
        summary["correct"] += result["correct"]
        summary["missed"] += result["absent"]
        summary["wrong"] += not result["correct"] and not result["absent"]
    for summary in fields.values():
        summary["recall"] = summary["correct"] / summary["expected"]
    normal = counts["normal"]
    normal["routing"] = {
        "numerator": normal["routed"], "denominator": normal["assessed"],
        "rate": normal["routed"] / normal["assessed"] if normal["assessed"] else None,
    }
    report = {
        "scope": "legacy" if legacy else "phase_two", "passed": not failures,
        "coverage": {
            "target_total": 125, "target_present": 125 - len(missing),
            "target_exercised": 55 if legacy else 125,
            "missing_target_codes": missing,
            "unassessed_target_codes": missing if legacy else [],
            "gene_total": 50, "gene_present": sum(gene["code"] in codes for gene in genes),
            "gene_counted_as_target": 0,
        },
        "counts": counts, "fields": fields, "result_types": result_types, "failures": failures,
    }
    return report, checks


def run_fixed_regression(dictionary, *, baseline=None, observer=None, baseline_observer=None):
    """Return a deterministic JSON-compatible report; failures always fail closed.

    ``baseline`` is an IndicatorDictionary, not a previously generated report.
    Both dictionaries are exercised now. Historical parser comparisons require
    running the frozen corpus with that parser; this function does not claim
    that a dictionary baseline freezes historical parser code.
    """
    if not isinstance(dictionary, IndicatorDictionary) or baseline is not None and not isinstance(baseline, IndicatorDictionary):
        raise TypeError("fixed_regression_requires_indicator_dictionary")
    corpus, corpus_sha256 = _load_corpus()
    parser_files = {name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest() for name in _PARSER_FILES}
    runtime = {"python": platform.python_version(), "unicode": unicodedata.unidata_version}
    report, checks = _evaluate(dictionary, corpus, observer=observer)
    report.update({
        "schema_version": "1.0", "dataset_kind": "SYNTHETIC", "corpus_id": corpus["corpus_id"],
        "corpus_sha256": corpus_sha256, "parser_sha256": _digest({"files": parser_files, "runtime": runtime}),
        "parser_files": parser_files, "runtime": runtime,
        "dictionary_version": dictionary.version, "dictionary_sha256": dictionary.content_hash,
        "dictionary_definition_sha256": _definition_digest(dictionary),
        "real_accuracy": "not_evaluated", "limits": corpus["limits"], "baseline": None,
    })
    if baseline is not None:
        baseline_report, baseline_checks = _evaluate(baseline, corpus, observer=baseline_observer)
        fields = {}
        for key in sorted(checks.keys() & baseline_checks.keys()):
            name = key[2]
            summary = fields.setdefault(name, {"expected": 0, "candidate_correct": 0, "baseline_correct": 0})
            summary["expected"] += 1
            summary["candidate_correct"] += checks[key]["correct"]
            summary["baseline_correct"] += baseline_checks[key]["correct"]
        decreased = []
        for name, summary in fields.items():
            summary["candidate_recall"] = summary["candidate_correct"] / summary["expected"]
            summary["baseline_recall"] = summary["baseline_correct"] / summary["expected"]
            if summary["candidate_correct"] < summary["baseline_correct"]:
                decreased.append(name)
                report["failures"].append({"case_id": "baseline:" + name, "reason": "field_recall_decreased", "field": name})
        report["baseline"] = {
            "scope": baseline_report["scope"], "dictionary_sha256": baseline.content_hash,
            "dictionary_definition_sha256": _definition_digest(baseline),
            "comparison": "identical_assessed_case_fields_current_parser",
            "fields": fields, "decreased_fields": sorted(decreased),
            "baseline_passed": baseline_report["passed"],
        }
        report["passed"] = not report["failures"]
    return report
