"""Frozen original-first imaging FIELD evaluation; public output is counts/hashes only."""

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _value(field):
    value = field["value"]
    key = field["field_key"]
    if value is None:
        return None
    if key in {"imaging.modality", "lesion.laterality"}:
        return {"code": value["code"]}
    if key == "lesion.dimensions":
        # Ordered values, original unit, approximate flag, axis and time role
        # remain strict. Formatting of the redundant raw rendering is separate.
        return {name: value[name] for name in ("components", "approximate", "measurement_role")}
    if "text" in value:
        return {"text": normalized(value["text"])}
    return value


def _quote(fields, *, gold=False):
    site = next((f for f in fields if f["field_key"] == "lesion.site"), fields[0] if fields else {})
    return normalized("\n".join(s.get("raw_quote" if gold else "raw_text", "") for s in site.get("sources" if gold else "fragments", [])))


def _anchor_present(anchor, source):
    anchor = normalized(anchor)
    if not anchor:
        return False
    # Allow a composed explicit organ/segment in the same clause. Never erase
    # laterality, digits, or missing anatomy characters to improve a score.
    return re.search(".{0,8}?".join(re.escape(char) for char in anchor), source) is not None


def _entity_pairs(expected, actual):
    gold, predictions = defaultdict(list), defaultdict(list)
    for field in expected:
        gold[field["entity_key"]].append(field)
    for field in actual:
        predictions[field["entity_key"]].append(field)
    edges = []
    for entity, fields in gold.items():
        if entity == "report":
            continue
        quote = _quote(fields, gold=True)
        site = next((f["value"]["text"] for f in fields if f["field_key"] == "lesion.site" and f["status"] == "PRESENT"), "")
        for candidate, candidate_fields in predictions.items():
            if candidate == "report":
                continue
            source = _quote(candidate_fields)
            if not _anchor_present(site, source):
                continue
            score = SequenceMatcher(None, quote, source, autojunk=False).ratio()
            if score >= .35:
                edges.append((score, entity, candidate))
    pairs, used, ambiguous = {"report": "report"}, set(), set()
    for score, entity, candidate in sorted(edges, key=lambda item: (-item[0], item[1], item[2])):
        if entity in pairs or entity in ambiguous or candidate in used:
            continue
        ties = [edge for edge in edges if edge[1] == entity and edge[0] == score and edge[2] not in used]
        if len(ties) > 1:
            ambiguous.add(entity)
            continue
        pairs[entity] = candidate
        used.add(candidate)
    return pairs, sorted(ambiguous)


def _metrics(counts):
    result = {key: counts[key] for key in ("correct", "mismatched", "missing", "extra")}
    precision = result["correct"] + result["mismatched"] + result["extra"]
    recall = result["correct"] + result["mismatched"] + result["missing"]
    result.update(precision_denominator=precision, recall_denominator=recall,
                  precision=result["correct"] / precision if precision else None,
                  recall=result["correct"] / recall if recall else None,
                  f1=2 * result["correct"] / (precision + recall) if precision + recall else None)
    return result


def evaluate_fields(gold, predictions):
    expected_numbers = set(gold.get("policy", {}).get("source_numbers", [])) or {r["source_number"] for r in gold["reports"]}
    actual = {source["source_number"]: source for source in predictions}
    if set(actual) != expected_numbers or len(actual) != len(predictions):
        raise ValueError("Every declared source must have one prediction, including failures")
    totals, absent, boundaries = Counter(), Counter(), Counter()
    by_field, audit, used_reports = defaultdict(Counter), [], set()
    for report in gold["reports"]:
        number = report["source_number"]
        pages = {p for left, right in report["page_ranges"] for p in range(left, right + 1)}
        candidates = [(index, row) for index, row in enumerate(actual[number]["reports"])
                      if (number, index) not in used_reports and set(row["pages"]) & pages and row["routing_kind"] == report["routing_kind"]]
        candidates.sort(key=lambda item: (set(item[1]["pages"]) != pages, -len(set(item[1]["pages"]) & pages), item[0]))
        exact_boundaries = [candidate for candidate in candidates if set(candidate[1]["pages"]) == pages]
        selected = candidates[0] if len(candidates) == 1 else exact_boundaries[0] if len(exact_boundaries) == 1 else None
        fields, pairs, ambiguous = [], {}, []
        source_boundary_valid = False
        if selected:
            index, prediction = selected
            used_reports.add((number, index))
            fields = prediction["fields"]
            source_boundary_valid = set(prediction["pages"]) == pages
            pairs, ambiguous = _entity_pairs(report["fields"], fields)
        boundaries["correct" if source_boundary_valid else "mismatched" if selected else "missing"] += 1
        used_fields, entries = set(), []
        for expected in report["fields"]:
            options = [(i, f) for i, f in enumerate(fields) if i not in used_fields
                       and f["entity_key"] == pairs.get(expected["entity_key"]) and f["field_key"] == expected["field_key"]]
            if expected["status"] == "ABSENT_NOT_STATED":
                absent["contradicted" if options else "not_filled" if expected["entity_key"] in pairs else "entity_unmatched"] += 1
                continue  # emitted values stay extra, not silently consumed
            if expected["status"] != "PRESENT":
                raise ValueError("Undeclared gold target status")
            options.sort(key=lambda pair: (_value(pair[1]) != _value(expected), pair[0]))
            matched = options[0] if options else None
            value_valid = source_valid = False
            if matched:
                i, field = matched
                used_fields.add(i)
                value_valid = _value(field) == _value(expected)
                expected_pages = {s["page"] for s in expected["sources"]}
                fragment_pages = {s["page"] for s in field.get("fragments", [])}
                source_valid = (source_boundary_valid and field.get("source_valid", False) and bool(fragment_pages)
                                and fragment_pages <= expected_pages)
            outcome = "missing" if matched is None else "correct" if value_valid and source_valid else "mismatched"
            totals[outcome] += 1
            by_field[expected["field_key"]][outcome] += 1
            entries.append({"entity_key": expected["entity_key"], "field_key": expected["field_key"], "outcome": outcome,
                            "prediction_index": matched[0] if matched else None, "value_correct": value_valid, "source_valid": bool(source_valid)})
        for i, field in enumerate(fields):
            if i not in used_fields:
                totals["extra"] += 1
                by_field[field["field_key"]]["extra"] += 1
        audit.append({"report_id": report["report_id"], "source_number": number, "predicted_report_index": selected[0] if selected else None,
                      "entity_pairs": pairs, "ambiguous_entities": ambiguous, "fields": entries})
    for number, source in actual.items():
        for index, report in enumerate(source["reports"]):
            if (number, index) not in used_reports:
                boundaries["extra"] += 1
                for field in report["fields"]:
                    totals["extra"] += 1
                    by_field[field["field_key"]]["extra"] += 1
    return {"files": {"total": len(actual), "failed": sum(source["status"] == "FAILED" for source in actual.values()),
                      "without_reports": sum(not source["reports"] for source in actual.values())},
            "reports": dict(boundaries), "fields": _metrics(totals), "by_field": {key: _metrics(value) for key, value in sorted(by_field.items())},
            "absent_targets": dict(absent), "ambiguous_entity_assignments": sum(len(r["ambiguous_entities"]) for r in audit),
            "review_burden": {"candidates_to_check": totals["correct"] + totals["mismatched"] + totals["extra"],
                              "candidates_needing_correction_or_removal": totals["mismatched"] + totals["extra"],
                              "annotated_fields_needing_manual_entry": totals["missing"]}}, audit


def predict_fields(manifest, *, dictionary=None, progress=None):
    from apps.documents.models import Document
    from apps.facts.clinical_readmodels import effective_field
    from tools.phase_three_evaluation import predict_sources

    files = [{"source_number": s["source_number"], "source_file_hash": s["source_sha256"], "source_path": s["source_path"],
              "ocr_cache_path": s["ocr_path"], "ocr_cache_sha256": s["ocr_sha256"],
              "ocr_pages": len(json.loads(Path(s["ocr_path"]).read_text(encoding="utf-8"))["pages"])} for s in manifest["sources"]]
    _, execution = predict_sources({"files": files}, dictionary=dictionary, progress=progress)
    output = []
    for source in files:
        document = Document.objects.get(sha256=source["source_file_hash"])
        version = document.parsing_versions.filter(active=True).first()
        extraction = getattr(version, "clinical_extraction", None) if version else None
        reports = []
        if version:
            for report in version.clinical_reports.order_by("ordinal", "pk"):
                fields = []
                for fact in report.fields.order_by("reading_order", "pk"):
                    effective = effective_field(fact)
                    fragments = [{"page": f.document_page.page_number, "reading_order": f.ocr_block.reading_order if f.ocr_block_id else None,
                                  "start_offset": f.start_offset, "end_offset": f.end_offset, "raw_text": f.raw_text, "polygon": f.polygon}
                                 for f in fact.source_fragments.select_related("document_page", "ocr_block").order_by("ordinal")]
                    fields.append({"field_key": fact.field_key, "entity_key": fact.entity_key, "value": fact.automatic_content["value"],
                                   "raw_text": fact.raw_text, "source_valid": effective["source_valid"], "fragments": fragments})
                reports.append({"ordinal": report.ordinal, "pages": sorted(set(report.spans.values_list("document_page__page_number", flat=True))),
                                "routing_kind": report.routing_kind, "fields": fields})
        output.append({"source_number": source["source_number"], "status": extraction.status if extraction else "FAILED",
                       "unparsed_page_count": extraction.unparsed_page_count if extraction else source["ocr_pages"], "reports": reports})
    return output, execution


def main(argv=None):
    import argparse
    import os
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if file_hash(args.gold) != args.gold_sha256:
        raise ValueError("Frozen field gold identity changed")
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not gold["policy"]["status"].startswith("FROZEN_PRE_PREDICTION") or gold["policy"]["prediction_read_before_freeze"]:
        raise ValueError("Original-first frozen annotations are required")
    declared = {s["source_number"]: s for s in gold["sources"]}
    if len(declared) != len(manifest["sources"]) or set(declared) != {s["source_number"] for s in manifest["sources"]}:
        raise ValueError("Source scope differs from frozen gold")
    for source in manifest["sources"]:
        expected = declared[source["source_number"]]
        if any(source[key] != expected[key] for key in ("source_sha256", "ocr_sha256")):
            raise ValueError("Source/ocr declared identity differs")
        if file_hash(source["source_path"]) != source["source_sha256"] or file_hash(source["ocr_path"]) != source["ocr_sha256"]:
            raise ValueError("Frozen original or OCR bytes changed")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connection
    if connection.vendor != "sqlite" or str(connection.settings_dict["NAME"]) != ":memory:":
        raise ValueError("CLI requires its own ephemeral in-memory database")
    paths = sorted({*root.glob("apps/facts/*.py"), *root.glob("apps/processing/*.py"), *root.glob("apps/processing/ocr/*.py"),
                    *root.glob("apps/labs/*.py"), root / "tools/clinical_field_evaluation.py", root / "tools/phase_three_evaluation.py", root / "tools/phase_two_evaluation.py"})
    identity = {p.relative_to(root).as_posix(): file_hash(p) for p in paths}
    if args.private_output.exists() or args.report.exists():
        raise ValueError("Choose a fresh output directory; retained evaluations are immutable")
    for path in paths:
        target = args.private_output / "source_snapshot" / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    call_command("migrate", verbosity=0)
    predictions, execution = predict_fields(manifest, progress=lambda done, total, status: print(f"{done}/{total} {status}", flush=True))
    metrics, audit = evaluate_fields(gold, predictions)
    if identity != {p.relative_to(root).as_posix(): file_hash(p) for p in paths}:
        raise ValueError("Application changed during replay")
    for path in (args.private_output / "predictions.json", args.private_output / "assignments.json", args.report):
        if path.exists():
            raise ValueError("Do not overwrite a retained evaluation; choose a new output directory")
    args.private_output.mkdir(parents=True, exist_ok=True)
    for name, payload in (("predictions", predictions), ("assignments", audit)):
        (args.private_output / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    report = {"schema_version": 1, "task_representation": "FIELD", "scope": gold["policy"]["declared_task"],
              "gold_counts": dict(Counter(field["status"] for r in gold["reports"] for field in r["fields"])),
              "current": metrics, "unparsed_pages": sum(p["unparsed_page_count"] for p in predictions),
              "identity": {"gold_sha256": args.gold_sha256, "manifest_sha256": file_hash(args.manifest), "parser_files": identity,
                           "prediction_content_sha256": file_hash(args.private_output / "predictions.json"),
                           "dictionary_version": execution["dictionary_version"], "dictionary_hash": execution["dictionary_hash"]},
              "method": {"entity_assignment": "one-to-one source-clause overlap, with all literal anatomy characters required in the candidate source; ambiguous ties do not receive credit",
                         "value_equality": "NFKC/whitespace for text; exact ordered numeric strings, original units, axes, approximate flag and time role; code-only coded values",
                         "source_equality": "same report page boundary plus immutable original OCR fragments on annotated source pages; this does not claim original-image glyph recognition",
                         "missing_and_failed_sources_retained": True, "legacy_excerpt_gold_unchanged": True,
                         "development_set_not_held_out": True}}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report["current"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
