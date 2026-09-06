"""Frozen fact-transcription evaluation. Public reports contain counts and hashes only."""
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def normalized(text):
    # Do not erase punctuation, negation, uncertainty, dates, stage or dose.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def body(prediction, gold):
    text = prediction["text"].strip()
    heading = gold.get("heading", "")
    if heading:
        text = re.sub(r"^" + re.escape(heading) + r"(?:\s*[:：]\s*|\s*\n)", "", text, count=1)
    return normalized(text)


def validate_inputs(inventory, annotations):
    sources, gold = inventory["files"], annotations["sources"]
    ids = [row["source_number"] for row in sources]
    expected = [row["source_number"] for row in gold]
    if annotations.get("status") != "frozen" or len(set(ids)) != len(ids) or len(set(expected)) != len(expected) or set(ids) != set(expected):
        raise ValueError("A frozen one-to-one annotation for every inventory source is required")
    pages = {row["source_number"]: row["ocr_pages"] for row in sources}
    for row in gold:
        count = pages[row["source_number"]]
        if not row.get("annotation_complete") or any(not 1 <= fact["page"] <= count for fact in row["facts"]):
            raise ValueError("Incomplete annotation or a fact outside its original source pages")
        judged, unknown = set(row.get("reviewed_pages", [])), set(row.get("unjudged_pages", []))
        if judged & unknown or judged | unknown != set(range(1, count + 1)):
            raise ValueError("Every page must be explicitly judged or unjudged")


def _assignment(gold, candidates):
    """Exact matches first, then deterministic one-to-one overlap for mismatch accounting."""
    edges = []
    for i, expected in enumerate(gold):
        target = normalized(expected["text"])
        for j, actual in enumerate(candidates):
            actual_text = body(actual, expected)
            same_text = actual_text == target
            same_source = actual.get("source_valid", True) and actual["page"] == expected["page"]
            same_category = actual["category"] == expected["category"]
            correct = same_text and same_source and same_category
            overlap = SequenceMatcher(None, target, actual_text, autojunk=False).ratio()
            # Similar text on a wrong page/category is a mismatch, not an exact hit.
            if same_text or overlap >= .35:
                score = (correct, same_text, same_source, same_category, overlap)
                edges.append((score, i, j))
    pairs, used = {}, set()
    for score, i, j in sorted(edges, key=lambda row: (row[0], -row[1], -row[2]), reverse=True):
        if i not in pairs and j not in used:
            pairs[i] = (j, bool(score[0]))
            used.add(j)
    return pairs, used


def evaluate_predictions(annotations, predictions):
    gold_map = {row["source_number"]: row for row in annotations}
    actual_map = {row["source_number"]: row for row in predictions}
    if len(gold_map) != len(annotations) or len(actual_map) != len(predictions) or set(gold_map) != set(actual_map):
        raise ValueError("Predictions must include every source exactly once, including failures")
    fields = dict(correct=0, mismatched=0, missing=0, extra=0, unjudgeable_predictions=0, unjudgeable_source_fields=0)
    files = dict(total=len(annotations), with_candidates=0, no_candidates=0, failed=0)
    dates = dict(correct=0, mismatched=0, missing_candidate=0, unannotated=0)
    unjudged_pages = total_candidates = 0
    for number, source in gold_map.items():
        actual = actual_map[number]
        all_candidates = actual["facts"]
        total_candidates += len(all_candidates)
        failed = actual["status"] == "FAILED"
        files["failed" if failed else "with_candidates" if all_candidates else "no_candidates"] += 1
        unknown = set(source.get("unjudged_pages", []))
        unjudged_pages += len(unknown)
        candidates = [item for item in all_candidates if item["page"] not in unknown]
        fields["unjudgeable_predictions"] += len(all_candidates) - len(candidates)
        fields["unjudgeable_source_fields"] += len(source.get("unjudgeable_fields", []))
        pairs, used = _assignment(source["facts"], candidates)
        for i, expected in enumerate(source["facts"]):
            match = pairs.get(i)
            fields["missing" if match is None else "correct" if match[1] else "mismatched"] += 1
            if "record_date" not in expected:
                dates["unannotated"] += 1
            elif match is None:
                dates["missing_candidate"] += 1
            else:
                actual_date = candidates[match[0]].get("record_date") or {"value": None, "precision": "UNKNOWN"}
                expected_date = expected["record_date"]
                dates["correct" if all(actual_date.get(key) == expected_date.get(key) for key in ("value", "precision")) else "mismatched"] += 1
        fields["extra"] += len(candidates) - len(used)
    precision_denominator = fields["correct"] + fields["mismatched"] + fields["extra"]
    recall_denominator = fields["correct"] + fields["mismatched"] + fields["missing"]
    fields.update(precision=fields["correct"]/precision_denominator if precision_denominator else None,
                  recall=fields["correct"]/recall_denominator if recall_denominator else None,
                  precision_denominator=precision_denominator, recall_denominator=recall_denominator)
    return dict(files=files, fields=fields, record_dates=dates, unjudged_pages=unjudged_pages,
        review_burden=dict(candidates_to_check=total_candidates,
            candidates_needing_correction_or_removal=fields["mismatched"]+fields["extra"],
            annotated_fields_needing_manual_entry=fields["missing"],
            unjudgeable_candidates=fields["unjudgeable_predictions"]))


def grouped_results(annotations, predictions, key):
    groups = sorted({row[key] for row in annotations})
    return {group: evaluate_predictions([row for row in annotations if row[key] == group],
        [row for row in predictions if row["source_number"] in {x["source_number"] for x in annotations if x[key] == group}])
        for group in groups}


def predict_sources(inventory, *, progress=None):
    """Use actual pipeline persistence against frozen OCR, exclusively in an empty memory DB."""
    from django.db import connection
    from apps.documents.models import Document
    from apps.facts.models import Fact, FactExtraction
    from apps.labs.dictionary import default_dictionary
    from tools.phase_two_evaluation import predict_frozen_sources
    if connection.vendor != "sqlite" or "memory" not in str(connection.settings_dict["NAME"]) or Document.objects.exists():
        raise ValueError("Fact evaluation requires an empty, ephemeral in-memory SQLite database")
    files = inventory["files"]
    roots = {str(Path(row["ocr_cache_path"]).parent) for row in files}
    if len(roots) != 1:
        raise ValueError("Frozen OCR caches must share a single private directory")
    sources = [{"hash": row["source_file_hash"], "path": row["source_path"]} for row in files]
    # Metadata is still predicted by the application, never supplied from the fact gold labels.
    baseline = {"samples": [{key: row[key] for key in ("source_file_hash", "ocr_cache_sha256", "ocr_pages")} for row in files]}
    classification = {"files": [{"source_file_hash": row["source_file_hash"]} for row in files]}
    replay = predict_frozen_sources(sources, next(iter(roots)), baseline, classification, default_dictionary(), progress=progress)
    output = []
    for source in files:
        document = Document.objects.get(sha256=source["source_file_hash"])
        version = document.parsing_versions.filter(active=True).first()
        extraction = FactExtraction.objects.filter(parsing_version=version).first() if version else None
        facts = []
        for fact in Fact.objects.filter(parsing_version=version).select_related("document_page", "evidence") if version else []:
            facts.append(dict(page=fact.document_page.page_number, category=fact.category, text=fact.raw_text,
                record_date=fact.automatic_content.get("record_date"), content=fact.automatic_content,
                source_valid=bool(fact.evidence_id and fact.evidence.parsing_version_id == version.pk
                    and fact.evidence.document_page_id == fact.document_page_id and fact.evidence.source_text == fact.raw_text),
                location="REGION" if fact.evidence_id and fact.evidence.polygon else "PAGE"))
        output.append(dict(source_number=source["source_number"], status=extraction.status if extraction else "FAILED", facts=facts))
    return output, replay["execution"]


def main(argv=None):
    import argparse
    import os
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--annotation-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if file_digest(args.annotations) != args.annotation_sha256:
        raise ValueError("Frozen fact annotation identity changed")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    gold = json.loads(args.annotations.read_text(encoding="utf-8"))
    validate_inputs(inventory, gold)
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from apps.facts.extraction import EXTRACTOR_VERSION
    if connection.vendor != "sqlite" or str(connection.settings_dict["NAME"]) != ":memory:":
        raise ValueError("CLI must initialize its own in-memory test database")
    paths = sorted([*root.glob("apps/facts/*.py"), *root.glob("apps/processing/*.py"),
                    *root.glob("apps/processing/ocr/*.py"), root/"tools/phase_three_evaluation.py"])
    identity = {path.relative_to(root).as_posix(): file_digest(path) for path in paths}
    call_command("migrate", verbosity=0)
    predictions, execution = predict_sources(inventory, progress=lambda done,total,status: print(f"{done}/{total} {status}",flush=True))
    annotations = gold["sources"]
    indexed = {row["source_number"]: row for row in inventory["files"]}
    for row in annotations:
        correction = gold.get("classification_review", {}).get(str(row["source_number"]), {})
        row["document_type"] = correction.get("document_type", indexed[row["source_number"]]["document_type"])
        row["report_group_ids"] = correction.get("report_group_ids", indexed[row["source_number"]]["report_group_ids"])
    if identity != {path.relative_to(root).as_posix(): file_digest(path) for path in paths}:
        raise ValueError("Application source changed during replay")
    report = dict(schema_version=1, scope="real_source_development_evaluation", extractor_version=EXTRACTOR_VERSION,
        current=evaluate_predictions(annotations,predictions), by_document_type=grouped_results(annotations,predictions,"document_type"),
        report_groups=len({group for row in annotations for group in row["report_group_ids"]}),
        source_pages=sum(row["ocr_pages"] for row in inventory["files"]),
        identity=dict(annotations_sha256=args.annotation_sha256, inventory_sha256=file_digest(args.inventory),
                      predictions_sha256=digest(predictions), parser_files=identity,
                      source_hashes=sorted(row["source_file_hash"] for row in inventory["files"])),
        limits=["Originals and frozen OCR bytes verified. Actual pipeline persistence executed; no new OCR timing claim.",
                "Development set, no holdout, no external clinical-accuracy claim or arbitrary accuracy threshold.",
                "Recall covers frozen judged fields only. Unjudged pages and clipped source fields are reported explicitly.",
                "Whitespace and NFKC normalization only; a heading may be omitted. Negation and context must remain.",
                "One-to-one candidates: whole narratives, explicit fields, and individual medication-order rows differ in size.",
                "Report dates scored separately; event-date association and institution correctness require original review.",
                "Every fact candidate needs original comparison before confirmation. Counts are workload proxies, not timed user studies."],
        execution=dict(database=execution["database"], files=len(predictions), ocr_replayed=True, persistence=True,
                       seconds=execution["total_seconds"]))
    for name, value in [("by_category","category"), ("by_unit","unit")]:
        # Assignments are computed within category/unit for a coverage view; do not sum these as a second total.
        if name=="by_category":
            values=("DIAGNOSIS","STAGE","TREATMENT","IMAGING","PATHOLOGY")
        else:
            values=("narrative_or_field","medication_order")
        report[name]={}
        for category in values:
            selected_gold=[{**row,"facts":[fact for fact in row["facts"] if fact.get(value,"narrative_or_field")==category],
                "unjudgeable_fields":[field for field in row.get("unjudgeable_fields",[])
                    if field.get(value,"medication_order" if row["document_type"]=="medication_orders" else "narrative_or_field")==category]}
                for row in annotations]
            if value=="category":
                selected_predictions=[{**row,"facts":[fact for fact in row["facts"] if fact[value]==category]} for row in predictions]
            else:
                numbers={row["source_number"] for row in annotations
                    if (row["document_type"]=="medication_orders")== (category=="medication_order")}
                selected_predictions=[{**row,"facts":row["facts"] if row["source_number"] in numbers else []} for row in predictions]
            report[name][category]=evaluate_predictions(selected_gold,selected_predictions)
    args.private_output.mkdir(parents=True,exist_ok=True)
    (args.private_output/"current-fact-predictions.json").write_text(json.dumps(predictions,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report["current"],ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
