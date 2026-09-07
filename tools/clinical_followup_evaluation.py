"""Separate frozen SUV/comparison task; the original seven-field scorer is unchanged."""

from collections import Counter, defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.clinical_field_evaluation import _anchor_present, _metrics, evaluate_fields, file_hash, normalized


EVALUATOR_VERSION = "clinical-imaging-followup-source-v1"
FIELD_KEYS = ("lesion.suvmax", "lesion.maximum_scope", "comparison.statement", "comparison.reference_date")


def project_fields(predictions, keys):
    """Explicit task projection retains every source, report and failure."""
    projected = deepcopy(predictions)
    for source in projected:
        for report in source["reports"]:
            report["fields"] = [field for field in report["fields"] if field["field_key"] in keys]
    return projected


def _quote(field, *, gold=False):
    return normalized("\n".join(source.get("raw_quote" if gold else "raw_text", "")
                                  for source in field.get("sources" if gold else "fragments", [])))


def _value(field):
    value = field["value"]
    if field["field_key"] == "lesion.suvmax":
        return {key: value[key] for key in ("values", "comparator", "unit", "approximate", "measurement_role")}
    if field["field_key"] == "lesion.maximum_scope":
        return {"code": value["code"]}
    if "text" in value:
        return {"text": normalized(value["text"])}
    return value


def _pair_entities(expected, actual):
    by_expected, by_actual = defaultdict(list), defaultdict(list)
    for field in expected:
        by_expected[field["entity_key"]].append(field)
    for field in actual:
        by_actual[field["entity_key"]].append(field)
    edges = []
    for entity, fields in by_expected.items():
        for proposed, candidates in by_actual.items():
            similarities = []
            for field in fields:
                for candidate in candidates:
                    if field["field_key"] != candidate["field_key"]:
                        continue
                    anchor = field.get("entity_anchor")
                    if anchor and not _anchor_present(anchor, _quote(candidate)):
                        continue
                    similarities.append(SequenceMatcher(None, _quote(field, gold=True), _quote(candidate), autojunk=False).ratio())
            if similarities and max(similarities) >= .35:
                edges.append((max(similarities), entity, proposed))
    pairs, used, ambiguous = {}, set(), set()
    for score, entity, proposed in sorted(edges, key=lambda row: (-row[0], row[1], row[2])):
        if entity in pairs or entity in ambiguous or proposed in used:
            continue
        ties = [row for row in edges if row[0] == score and row[1] == entity and row[2] not in used]
        if len(ties) != 1:
            ambiguous.add(entity)
            continue
        pairs[entity] = proposed
        used.add(proposed)
    return pairs, ambiguous


def field_source_proof(expected, actual, boundary_valid):
    fragments, sources = actual.get("fragments", []), expected.get("sources", [])
    pages = {source["page"] for source in sources}
    if (not boundary_valid or actual.get("source_valid") is not True or not fragments
            or not {fragment["page"] for fragment in fragments} <= pages):
        return "INVALID", "source_or_report_boundary_invalid"
    for source in sources:
        on_page = [fragment for fragment in fragments if fragment["page"] == source["page"]]
        if not on_page:
            return "UNVERIFIED", "annotated_page_not_represented"
        locator, polygon = source.get("ocr_offsets"), source.get("exact_polygon")
        if locator is not None:
            if not isinstance(locator, dict) or set(locator) != {"reading_order", "start_offset", "end_offset"}:
                return "UNVERIFIED", "unsupported_frozen_locator"
            if not any(all(fragment.get(key) == value for key, value in locator.items()) for fragment in on_page):
                return "INVALID", "frozen_character_locator_differs"
        if polygon is not None and not any(fragment.get("polygon") == polygon for fragment in on_page):
            return "INVALID", "frozen_original_polygon_differs"
        if locator is not None or polygon is not None:
            continue
        quote = normalized(source.get("raw_quote", ""))
        own = normalized("\n".join(fragment.get("raw_text", "") for fragment in on_page))
        if not quote or not own:
            return "UNVERIFIED", "own_field_source_missing"
        if expected["field_key"].startswith("comparison."):
            if own != quote:
                return "UNVERIFIED", "complete_comparison_quote_not_proven"
        else:
            # No broad source-presence credit: an overlong whole-report span,
            # another same-page site, or an organ word alone cannot prove SUV.
            if own not in quote or not _anchor_present(expected.get("entity_anchor", ""), own):
                return "UNVERIFIED", "own_source_within_frozen_entity_clause_not_proven"
            expression = normalized(expected["value"]["raw"])
            if not expression or expression not in own:
                return "UNVERIFIED", "own_quantitative_or_maximum_expression_not_proven"
            if expected["value"].get("measurement_role") == "HISTORICAL" and own != quote:
                return "UNVERIFIED", "historical_source_context_not_proven"
    return "VERIFIED", "own_field_frozen_locator_or_literal_quote"


def evaluate_followup(gold, predictions):
    if set(gold["policy"]["field_keys"]) != set(FIELD_KEYS):
        raise ValueError("This evaluator only scores the explicitly declared four new field keys")
    actual = {source["source_number"]: source for source in project_fields(predictions, FIELD_KEYS)}
    if set(actual) != set(gold["policy"]["source_numbers"]) or len(actual) != len(predictions):
        raise ValueError("Retain each declared source exactly once, including failures")
    totals, boundaries, absence = Counter(), Counter(), Counter()
    by_field, audits, used_reports = defaultdict(Counter), [], set()
    for report in gold["reports"]:
        pages = {page for left, right in report["page_ranges"] for page in range(left, right + 1)}
        number = report["source_number"]
        options = [(index, candidate) for index, candidate in enumerate(actual[number]["reports"])
                   if (number, index) not in used_reports and set(candidate["pages"]) & pages
                   and candidate["routing_kind"] == report["routing_kind"]]
        exact = [option for option in options if set(option[1]["pages"]) == pages]
        selected = exact[0] if len(exact) == 1 else options[0] if len(options) == 1 else None
        fields, pairs, ambiguous = [], {}, set()
        valid_boundary = False
        if selected:
            index, candidate = selected
            used_reports.add((number, index))
            fields = candidate["fields"]
            valid_boundary = set(candidate["pages"]) == pages
            pairs, ambiguous = _pair_entities(report["fields"], fields)
        boundaries["correct" if valid_boundary else "mismatched" if selected else "missing"] += 1
        used, entries = set(), []
        for gold_index, expected in enumerate(report["fields"]):
            if expected["field_key"] not in FIELD_KEYS or expected["status"] != "PRESENT":
                raise ValueError("Unsupported positive field target; do not silently omit gold")
            options = [(index, field) for index, field in enumerate(fields) if index not in used
                       and field["field_key"] == expected["field_key"] and field["entity_key"] == pairs.get(expected["entity_key"])]
            options.sort(key=lambda pair: (-SequenceMatcher(None, _quote(expected, gold=True), _quote(pair[1]), autojunk=False).ratio(), pair[0]))
            matched = options[0] if options else None
            source_status, source_reason, value_correct = "MISSING", "candidate_missing", False
            if matched:
                index, field = matched
                used.add(index)
                value_correct = _value(expected) == _value(field)
                source_status, source_reason = field_source_proof(expected, field, valid_boundary)
            outcome = "missing" if matched is None else "correct" if value_correct and source_status == "VERIFIED" else "mismatched"
            for bucket in (totals, by_field[expected["field_key"]]):
                bucket[outcome] += 1
                bucket["source_unverified"] += source_status == "UNVERIFIED"
                if matched:
                    bucket["value_correct" if value_correct else "value_mismatched"] += 1
            entries.append(dict(gold_index=gold_index, entity_key=expected["entity_key"], field_key=expected["field_key"],
                                prediction_index=matched[0] if matched else None, outcome=outcome, value_correct=value_correct,
                                source_status=source_status, source_reason=source_reason))
        for index, field in enumerate(fields):
            if index not in used:
                totals["extra"] += 1
                by_field[field["field_key"]]["extra"] += 1
        for absent in report.get("absence", []):
            if absent["status"] != "ABSENT_NOT_STATED_IN_REPORT" or absent["field_key"] not in FIELD_KEYS:
                raise ValueError("Undeclared negative scope")
            absence["contradicted" if any(field["field_key"] == absent["field_key"] for field in fields) else "not_filled"] += 1
        audits.append(dict(report_id=report["report_id"], source_number=number, predicted_report_index=selected[0] if selected else None,
                           entity_pairs=pairs, ambiguous_entities=sorted(ambiguous), fields=entries))
    for number, source in actual.items():
        for index, report in enumerate(source["reports"]):
            if (number, index) not in used_reports:
                boundaries["extra"] += 1
                for field in report["fields"]:
                    totals["extra"] += 1
                    by_field[field["field_key"]]["extra"] += 1
    return dict(files=dict(total=len(actual), failed=sum(source["status"] == "FAILED" for source in actual.values())),
                reports=dict(boundaries), fields=_metrics(totals), by_field={key: _metrics(by_field[key]) for key in FIELD_KEYS},
                absent_report_field_checks=dict(absence), review_burden=dict(candidates_to_check=totals["correct"] + totals["mismatched"] + totals["extra"],
                candidates_needing_correction_source_review_or_removal=totals["mismatched"] + totals["extra"],
                annotated_fields_needing_manual_entry=totals["missing"])), audits


def main(argv=None):
    import argparse
    import os
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "gold", "original-gold", "report", "private-output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--original-gold-sha256", required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--prediction-report", type=Path)
    args = parser.parse_args(argv)
    if bool(args.predictions) != bool(args.prediction_report):
        raise ValueError("Retained predictions and their generation report are required together")
    if file_hash(args.gold) != args.gold_sha256 or file_hash(args.original_gold) != args.original_gold_sha256:
        raise ValueError("Frozen gold bytes changed")
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    original_gold = json.loads(args.original_gold.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if gold["policy"]["status"] != "FROZEN_BEFORE_NEW_PREDICTIONS" or gold["policy"]["old_gold_sha256"] != args.original_gold_sha256:
        raise ValueError("New-task original-first freeze and unchanged original gold are required")
    expected = {source["source_number"]: source for source in gold["sources"]}
    if set(expected) != {source["source_number"] for source in manifest["sources"]} or len(expected) != len(manifest["sources"]):
        raise ValueError("Declared source scope differs")
    for source in manifest["sources"]:
        if any(source[key] != expected[source["source_number"]][key] for key in ("source_sha256", "ocr_sha256")):
            raise ValueError("Declared source identity differs")
        if file_hash(source["source_path"]) != source["source_sha256"] or file_hash(source["ocr_path"]) != source["ocr_sha256"]:
            raise ValueError("Frozen source or OCR bytes changed")
    paths = sorted({*root.glob("apps/facts/*.py"), *root.glob("apps/processing/*.py"), *root.glob("apps/processing/ocr/*.py"),
                    *root.glob("apps/labs/*.py"), root / "tools/clinical_field_evaluation.py", Path(__file__).resolve(),
                    root / "tools/phase_three_evaluation.py", root / "tools/phase_two_evaluation.py"})
    identity = {path.relative_to(root).as_posix(): file_hash(path) for path in paths}
    if args.private_output.exists() or args.report.exists():
        raise ValueError("Choose fresh outputs; retained evaluations are immutable")
    original_identity = None
    if args.predictions:
        original_identity = json.loads(args.prediction_report.read_text(encoding="utf-8"))["identity"]
        checks = dict(gold_sha256=args.gold_sha256, original_gold_sha256=args.original_gold_sha256,
                      manifest_sha256=file_hash(args.manifest), prediction_content_sha256=file_hash(args.predictions))
        if any(original_identity[key] != value for key, value in checks.items()):
            raise ValueError("Retained prediction/gold/manifest identity differs from generation")
        predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
        execution = {key: original_identity[key] for key in ("dictionary_version", "dictionary_hash")}
    else:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import connection
        from tools.clinical_field_evaluation import predict_fields
        if connection.vendor != "sqlite" or str(connection.settings_dict["NAME"]) != ":memory:":
            raise ValueError("Replay requires its own in-memory database")
        call_command("migrate", verbosity=0)
        predictions, execution = predict_fields(manifest, progress=lambda done, total, status: print(f"{done}/{total} {status}", flush=True))
    metrics, audit = evaluate_followup(gold, predictions)
    old_keys = tuple(original_gold["policy"]["field_keys"])
    original_projection = project_fields(predictions, old_keys)
    original_metrics, original_audit = evaluate_fields(original_gold, original_projection)
    if identity != {path.relative_to(root).as_posix(): file_hash(path) for path in paths}:
        raise ValueError("Application or scorer changed during evaluation")
    args.private_output.mkdir(parents=True)
    for name, payload in (("predictions", predictions), ("assignments", audit), ("original-seven-predictions", original_projection), ("original-seven-assignments", original_audit)):
        (args.private_output / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if args.predictions:
        (args.private_output / "predictions.json").write_bytes(args.predictions.read_bytes())
    for path in ([Path(__file__).resolve(), root / "tools/clinical_field_evaluation.py"] if args.predictions else paths):
        target = args.private_output / ("scorer_snapshot" if args.predictions else "source_snapshot") / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    emitted = Counter(field["field_key"] for source in predictions for report in source["reports"] for field in report["fields"])
    report = dict(schema_version=1, evaluator_version=EVALUATOR_VERSION, task_representation="FIELD",
        execution_kind="retained_prediction_rescore" if args.predictions else "actual_pipeline_replay",
        scope=dict(new_field_keys=list(FIELD_KEYS), original_seven_projection=list(old_keys), all_emitted_by_field=dict(sorted(emitted.items())),
                   new_gold_counts=gold["summary"], unparsed_pages=sum(p["unparsed_page_count"] for p in predictions)),
        current=metrics, original_seven_fields=original_metrics,
        identity=dict(gold_sha256=args.gold_sha256, original_gold_sha256=args.original_gold_sha256, manifest_sha256=file_hash(args.manifest),
            parser_files=original_identity["parser_files"] if original_identity else identity,
            scorer_sha256=file_hash(__file__), original_seven_scorer_sha256=file_hash(root / "tools/clinical_field_evaluation.py"),
            original_generation_report_sha256=file_hash(args.prediction_report) if args.prediction_report else None,
            prediction_content_sha256=file_hash(args.private_output / "predictions.json"),
            original_seven_prediction_sha256=file_hash(args.private_output / "original-seven-predictions.json"),
            dictionary_version=execution["dictionary_version"], dictionary_hash=execution["dictionary_hash"]),
        method=dict(assignment="One-to-one report-local entity source similarity with required literal anchor; no value-based pairing; ties not credited.",
            source="Each field proves its own frozen quote or locator. Comparison source must be complete; scalar/maximum source must be within its original entity clause and contain the quantitative or maximum expression. Unverified stays a disclosed strict mismatch.",
            projection="New task scores only its four declared keys; all emitted fields remain in immutable predictions. Original seven-key projection retains every source/report and uses the unchanged prior scorer and gold.",
            values="Numeric strings, comparator, explicit unit, approximate/time role retained; coded category compared separately from raw wording; text NFKC/whitespace only.",
            development_set_not_held_out=True, no_real_sample_scope="Historical/ranged/inequality SUV, report-wide maximum and uncertain reference dates have synthetic contract coverage only."))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(dict(new_fields=metrics["fields"], original_seven_fields=original_metrics["fields"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
