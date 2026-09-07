"""Frozen treatment-source evaluation; public output contains only counts/hashes.

The independently reviewed corpus currently has no joint organizational positive.
Source dates and ordinal tokens are separate components, never surrogate cycles.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unicodedata


ANCHOR_KINDS = {"SYSTEMIC_TREATMENT", "CELL_THERAPY", "RADIOTHERAPY", "SURGERY", "LOCAL_PROCEDURE", "ADMISSION"}
NEGATIVE_SCOPES = {"LAB_ONLY", "LAB_OR_PATHOLOGY_ONLY", "PATHOLOGY_ONLY", "SUPPORTIVE_ORDERS",
                   "MOLECULAR_RESULTS_OR_BOILERPLATE", "MOLECULAR_RECOMMENDATION_NOT_ADMINISTRATION",
                   "MOLECULAR_RESULTS_OR_RECOMMENDATION", "SUPPORTIVE_OR_UNCLASSIFIED_ORDERS"}


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text or "").casefold()
                   if not char.isspace() and not unicodedata.category(char).startswith("P"))


def counts(tp=0, fp=0, fn=0, unknown=0):
    ratio = lambda numerator, denominator: numerator / denominator if denominator else None
    return {"TP": tp, "FP": fp, "FN": fn, "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "TP_over_TP_plus_FP_plus_FN": ratio(tp, tp + fp + fn), "unjudged_predictions": unknown,
            "judged_prediction_coverage": ratio(tp + fp, tp + fp + unknown)}


def _locations(row):
    return {(proof["source_number"], proof["page"]) for proof in row.get("sources", [])}


def _gold_events(gold):
    mentions = {event["mention_id"]: (source["source_number"], page["page"])
                for source in gold["sources"] for page in source["pages"] for event in page.get("events", [])}
    events = {row["id"]: row for row in gold["events"]}
    output = []
    for case in gold["partial_anchor_cases"]:
        row = events[case["event_id"]]
        output.append({**deepcopy(row), "case": case,
                       "locations": {mentions[key] for key in row["mention_ids"] if key in mentions},
                       "date": case.get("reported_event_day", row["date"])})
    return output


def _component(expected, actual, pages, *, name):
    if name != "regimen_texts":
        actual = [row for row in actual if row["content"].get("date") and row["content"].get("date_precision") == "DAY"]
    def eligible(row):
        case = row["case"]
        if name == "reported_event_dates":
            return case.get("event_day_judgable", False)
        if name == "literal_dates":
            return case.get("literal_date_judgable", case.get("event_day_judgable", False))
        return case.get("regimen_identity_judgable", not row.get("regimen_conflict", False)) and bool(row.get("regimen"))

    def source_match(left, right):
        return left["kind"] == right["content"]["kind"] and bool(left["locations"] & _locations(right))

    def same_value(left, right):
        content = right["content"]
        day = content["date"] == left["date"] and content["date_precision"] == left["date_precision"]
        if name != "regimen_texts":
            return day
        variants = left.get("regimen_variants") or [left.get("regimen")]
        return normalized(content.get("regimen_text")) in {normalized(value) for value in variants}

    targets = [row for row in expected if eligible(row)]
    unjudged = [row for row in expected if not eligible(row)]
    # Exact source/value matches get priority. Unknown truths reserve their exact
    # candidates before mismatched assignment, e.g. a legible but disputed year.
    edges = [(int(left["patient_group"] == right["patient_group"] and same_value(left, right)), i, j)
             for i, left in enumerate(targets) for j, right in enumerate(actual) if source_match(left, right)]
    matches, used = {}, set()
    for correct, i, j in sorted(edges, key=lambda edge: (-edge[0], edge[1], edge[2])):
        if not correct:
            continue
        if i not in matches and j not in used:
            matches[i] = (j, True)
            used.add(j)
    reserved = {j for j, row in enumerate(actual) if j not in used and any(
        left["patient_group"] == row["patient_group"] and source_match(left, row) and same_value(left, row) for left in unjudged)}
    for correct, i, j in edges:
        if i not in matches and j not in used and j not in reserved:
            matches[i] = (j, False)
            used.add(j)
    tp = sum(correct for _j, correct in matches.values())
    fp = sum(not correct for _j, correct in matches.values())
    fn = len(targets) - tp
    error_ids = {actual[j]["id"] for j, correct in matches.values() if not correct}
    trace = [{"gold_id": targets[i]["id"], "prediction_id": actual[j]["id"], "correct": correct}
             for i, (j, correct) in matches.items()]
    unknown = len(reserved)
    for j, row in enumerate(actual):
        if j in used or j in reserved:
            continue
        # Only explicit known source support is negative evidence. OCR-only or
        # partially annotated pages cannot silently become blanket negatives.
        duplicate_or_wrong = any(source_match(left, row) for left in targets)
        crossed = any(location in pages and pages[location]["patient_group"] != row["patient_group"] for location in _locations(row))
        negative = any(pages.get(location, {}).get("review_state") == "ORIGINAL_AND_OCR_REVIEWED"
                       and pages[location]["relevance"] in NEGATIVE_SCOPES for location in _locations(row))
        if duplicate_or_wrong or crossed or negative:
            fp += 1
            error_ids.add(row["id"])
            trace.append({"prediction_id": row["id"], "correct": False, "reason": "duplicate_or_unsupported_source_assertion"})
        else:
            unknown += 1
            trace.append({"prediction_id": row["id"], "correct": None, "reason": "original_component_not_judged"})
    return counts(tp, fp, fn, unknown), error_ids, trace


def score_predictions(gold, predictions):
    expected_files = {row["source_number"] for row in gold["sources"]}
    actual_files = [row["source_number"] for row in predictions["files"]]
    if set(actual_files) != expected_files or len(set(actual_files)) != len(actual_files):
        raise ValueError("Every original input, including failures, must appear once")
    if gold.get("cycles"):
        raise ValueError("This frozen unknown-boundary protocol requires independent review before scoring new joint positives")
    pages = {(source["source_number"], page["page"]): page for source in gold["sources"] for page in source["pages"]}
    expected = _gold_events(gold)
    events = [{**deepcopy(row), "patient_group": group["patient_group"]} for group in predictions["groups"] for row in group["events"]
              if row["content"]["kind"] in ANCHOR_KINDS and row["content"]["occurrence"] == "OCCURRED"]
    report, traces, errors = {}, {}, set()
    for name in ("reported_event_dates", "literal_dates", "regimen_texts"):
        report[name], wrong, traces[name] = _component(expected, events, pages, name=name)
        errors.update(wrong)
    labels = [{**deepcopy(row), "patient_group": group["patient_group"]} for group in predictions["groups"] for row in group["labels"]
              if row.get("ordinal") and not row.get("reason")]
    gold_labels = [row for row in gold.get("unlinked_cycle_labels", []) if row.get("original_ordinal_judgable")]
    used, ordinal_errors, ordinal_tp, ordinal_unknown, date_links_wrong = set(), 0, 0, 0, 0
    for target in gold_labels:
        locations = {(row["source_number"], row["page"]) for row in target["source_locations"]}
        candidates = [i for i, row in enumerate(labels) if i not in used and (row["source_number"], row["page"]) in locations]
        exact = [i for i in candidates if labels[i]["patient_group"] == target["patient_group"] and labels[i]["ordinal"] == target["cycle_ordinal"]]
        if candidates:
            chosen = (exact or candidates)[0]
            used.add(chosen)
            ordinal_tp += bool(exact)
            ordinal_errors += not bool(exact)
            if labels[chosen].get("event_date") is not None and not target.get("ordinal_to_event_date_judgable"):
                date_links_wrong += 1
    for i, row in enumerate(labels):
        if i in used:
            continue
        page = pages.get((row["source_number"], row["page"]), {})
        if any((location["source_number"], location["page"]) == (row["source_number"], row["page"])
               for target in gold_labels for location in target["source_locations"]):
            ordinal_errors += 1
        elif page.get("review_state") == "ORIGINAL_AND_OCR_REVIEWED" and page.get("relevance") in NEGATIVE_SCOPES:
            ordinal_errors += 1
        else:
            ordinal_unknown += 1
    report["original_ordinals"] = counts(ordinal_tp, ordinal_errors, len(gold_labels) - ordinal_tp, ordinal_unknown)
    report["ordinal_date_links"] = counts(fp=date_links_wrong)
    joint_wrong, joint_unknown = 0, 0
    for group in predictions["groups"]:
        for cycle in group["cycles"]:
            content = cycle["content"]
            wrong = bool(set(cycle["event_ids"]) & errors or content.get("true_d1_claimed") or content.get("end"))
            if content.get("ordinal") and content.get("anchor") and date_links_wrong:
                wrong = wrong or any(label.get("event_date") == content["anchor"] and label["ordinal"] == content["ordinal"] for label in labels)
            joint_wrong += wrong
            joint_unknown += not wrong
    report.update({"joint": counts(fp=joint_wrong, unknown=joint_unknown),
        "target_precision": 0.8, "target_status": "NOT_ESTABLISHED_INSUFFICIENT_JOINT_GOLD",
        "source_support_assertion_errors": len(errors) + ordinal_errors + date_links_wrong,
        "input_files": len(actual_files), "failed_inputs": sum(row["status"] == "failed" for row in predictions["files"]),
        "source_pages": len(pages), "original_visual_pending_pages": sum(row["review_state"] != "ORIGINAL_AND_OCR_REVIEWED" for row in pages.values()),
        "proposed_cycles": joint_wrong + joint_unknown, "raw_event_signals": sum(len(group["events"]) for group in predictions["groups"]),
        "raw_ordinal_tokens": len(labels), "regimen_proposals": sum(len(group["regimens"]) for group in predictions["groups"]),
        "reported_day_gold_kind_counts": dict(Counter(row["kind"] for row in expected if row["case"].get("event_day_judgable")))})
    cycle_sources = {proof["source_number"] for group in predictions["groups"] for event in group["events"]
                     if any(event["id"] in cycle["event_ids"] for cycle in group["cycles"]) for proof in event["sources"]}
    report["no_proposal_inputs"] = len(expected_files - cycle_sources)
    judged_locations = set().union(*(row["locations"] for row in expected if row["case"].get("event_day_judgable"))) if expected else set()
    observed_locations = set().union(*(_locations(row) for row in events)) if events else set()
    report["source_coverage"] = {"judged_reported_day_files": len({source for source, _page in judged_locations}),
        "judged_reported_day_pages": len(judged_locations), "predicted_event_files": len({source for source, _page in observed_locations}),
        "predicted_event_pages": len(observed_locations), "predicted_pages_with_judged_reported_days": len(observed_locations & judged_locations)}
    return report, traces


def predict_sources(manifest, patient_groups, dictionary, *, progress=None):
    """Prediction receives only frozen originals/OCR and original patient identity."""
    from django.db import connection
    from apps.documents.models import Document
    from apps.patients.models import Patient
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from tools.phase_two_evaluation import predict_frozen_sources
    import uuid

    if (connection.vendor != "sqlite" or str(connection.settings_dict["NAME"]) not in {":memory:", "file:memorydb_default?mode=memory&cache=shared"}
            or Document.objects.exists()):
        raise ValueError("Prediction requires a new empty in-memory evaluation database")
    sources = manifest["sources"]
    roots = {str(Path(row["ocr_path"]).parent) for row in sources}
    if len(roots) != 1:
        raise ValueError("Frozen OCR cache paths must have one unchanged root")
    baseline = {"samples": [{"source_file_hash": row["source_sha256"], "ocr_cache_sha256": row["ocr_sha256"], "ocr_pages": row["ocr_pages"]} for row in sources]}
    classification = {"files": [{"source_file_hash": row["source_sha256"], "patient_group_id": patient_groups[row["source_number"]]} for row in sources]}
    replay = predict_frozen_sources([{"hash": row["source_sha256"], "path": row["source_path"]} for row in sources],
                                    next(iter(roots)), baseline, classification, dictionary, progress=progress)
    by_hash = {row["source_sha256"]: row["source_number"] for row in sources}
    document_numbers = {str(row.pk): by_hash[row.sha256] for row in Document.objects.all()}
    groups = []
    for patient in Patient.objects.all().order_by("pk"):
        number = by_hash[Document.objects.filter(patient=patient).first().sha256]
        preview = proposal_preview(patient, actor=patient.account)
        proposals = deepcopy(preview["proposals"])
        source_ids = {}
        for event in proposals["events"]:
            for proof in event["sources"]:
                proof["source_number"] = document_numbers[proof["document_id"]]
                source_ids[proof["source_id"]] = (proof["source_number"], proof["page"])
        # Labels can exist in a source even when it yields no dated event.
        from apps.treatments.input_material import trusted_input_material
        from apps.patients.access import authorize_patient
        from django.db import transaction
        with transaction.atomic():
            authorize_patient(patient, patient.account, "read", lock=True)
            inputs = trusted_input_material(patient)
        for source in inputs["sources"]:
            source_ids[source["id"]] = (document_numbers[source["source"]["document_id"]], source["source"]["page"])
        for label in proposals["labels"]:
            label["source_number"], label["page"] = source_ids[label["source_id"]]
        # Exercise the actual authorized persistence path too; no confirmation or
        # gold answer is supplied. Automatic baseline storage is idempotent.
        run = persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())
        groups.append({"patient_group": patient_groups[number], "input_fingerprint": preview["input_fingerprint"],
                       "persisted_result_counts": run.result_counts, **proposals})
    output = {"files": [{"source_number": by_hash[row["source_file_hash"]], "status": row["status"]} for row in replay["predictions"]], "groups": groups}
    return output, replay["execution"]


def main(argv=None):
    import argparse
    import os
    import sys
    import subprocess
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "gold", "protocol", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("manifest", "gold", "protocol"):
        parser.add_argument("--" + name + "-sha256", required=True)
    args = parser.parse_args(argv)
    for name in ("manifest", "gold", "protocol"):
        if file_digest(getattr(args, name)) != getattr(args, name + "_sha256"):
            raise ValueError("Frozen input identity changed: " + name)
    manifest, gold, protocol = [json.loads(getattr(args, name).read_text(encoding="utf-8")) for name in ("manifest", "gold", "protocol")]
    if protocol.get("prediction_seen") is not False or gold.get("prediction_seen") is not False or gold["scoring_protocol_sha256"] != args.protocol_sha256:
        raise ValueError("Independent gold and scoring protocol must precede real prediction")
    original = {row["source_number"]: row for row in manifest["sources"]}
    if set(original) != {row["source_number"] for row in gold["sources"]}:
        raise ValueError("Gold and input source inventories differ")
    patient_groups = {}
    for source in gold["sources"]:
        actual = original[source["source_number"]]
        if any(source[key] != actual[key] for key in ("source_sha256", "ocr_sha256", "ocr_pages")):
            raise ValueError("Original/OCR identity or page count differs")
        groups = {page["patient_group"] for page in source["pages"]}
        if len(groups) != 1 or {page["page"] for page in source["pages"]} != set(range(1, source["ocr_pages"] + 1)):
            raise ValueError("Every page needs unambiguous original patient identity")
        patient_groups[source["source_number"]] = groups.pop()
        for path_key, hash_key in (("source_path", "source_sha256"), ("ocr_path", "ocr_sha256")):
            if file_digest(actual[path_key]) != actual[hash_key]:
                raise ValueError("Frozen original or OCR bytes changed")
    output = args.output.resolve()
    if not output.is_relative_to((root / ".runtime").resolve()) or output.exists():
        raise ValueError("Use a new private .runtime directory; previous evidence is immutable")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    import django
    django.setup()
    from django.core.management import call_command
    from apps.labs.dictionary import current_dictionary
    call_command("migrate", verbosity=0)
    dictionary = current_dictionary()
    paths = sorted([*root.glob("apps/**/*.py"), root / "tools/phase_two_evaluation.py", Path(__file__).resolve()])
    source_identity = {path.relative_to(root).as_posix(): file_digest(path) for path in paths}
    identity = {"frozen_at": datetime.now(timezone.utc).isoformat(), "prediction_seen": False,
                "manifest_sha256": args.manifest_sha256, "gold_sha256": args.gold_sha256, "protocol_sha256": args.protocol_sha256,
                "source_file_count": len(original), "page_count": sum(row["ocr_pages"] for row in original.values()),
                "original_and_ocr_hashes_verified": 2 * len(original), "patient_identity_groups": len(set(patient_groups.values())),
                "application_sources": source_identity, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                "dictionary_version": dictionary.version, "dictionary_hash": dictionary.content_hash}
    output.mkdir(parents=True)
    write = lambda name, value: (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    write("input-freeze.json", identity)
    predictions, execution = predict_sources(manifest, patient_groups, dictionary,
        progress=lambda done, total, status: print(f"{done}/{total} {status}", flush=True))
    write("predictions.json", predictions)
    report, traces = score_predictions(gold, predictions)
    write("private-matching-trace.json", traces)
    if source_identity != {path.relative_to(root).as_posix(): file_digest(path) for path in paths}:
        raise ValueError("Application source changed during frozen prediction")
    report.update({"input_freeze_sha256": file_digest(output / "input-freeze.json"), "predictions_sha256": file_digest(output / "predictions.json"),
                   "matching_trace_sha256": file_digest(output / "private-matching-trace.json"), "gold_sha256": args.gold_sha256,
                   "protocol_sha256": args.protocol_sha256, "manifest_sha256": args.manifest_sha256,
                   "scope": "Authorized development corpus; no independent holdout; unknown cycle truth cannot establish the 80% goal.",
                   "execution": {key: value for key, value in execution.items() if key != "files"}})
    write("public-report.json", report)
    print(json.dumps({"input_files": report["input_files"], "proposed_cycles": report["proposed_cycles"], "joint": report["joint"], "target_status": report["target_status"]}), flush=True)


if __name__ == "__main__":
    main()
