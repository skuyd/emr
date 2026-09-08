"""Frozen treatment-source evaluation; public output contains only counts/hashes.

The independently reviewed corpus currently has no joint organizational positive.
Source dates and ordinal tokens are separate components, never surrogate cycles.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.treatment_source_mapping import MAPPING_VERSION, SourceMapper, compact, spans_overlap, text_hash


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
    mentions = {event["mention_id"]: event
                for source in gold["sources"] for page in source["pages"] for event in page.get("events", [])}
    events = {row["id"]: row for row in gold["events"]}
    output = []
    for case in gold["partial_anchor_cases"]:
        row = events[case["event_id"]]
        output.append({**deepcopy(row), "case": case,
                       "mentions": [mentions[key] for key in row["mention_ids"] if key in mentions],
                       "locations": {(mentions[key]["source"]["source_number"], mentions[key]["source"]["page"])
                                     for key in row["mention_ids"] if key in mentions},
                       "date": case.get("reported_event_day", row["date"])})
    return output


_FULL_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})(?:\s*日)?(?!\d)")


def _literal_days(text):
    result = []
    for match in _FULL_DATE.finditer(text):
        try:
            value = date(*map(int, match.groups())).isoformat()
        except ValueError:
            continue
        result.append((match.start(), match.end(), value))
    return result


def _assertion_units(text):
    # These scoring units do not call the treatment extractor. Strong sentence
    # punctuation separates assertions; adjacent date lists stay together, but
    # a new date after intervening treatment prose starts a different assertion.
    for sentence in re.split(r"[。；;]", compact(text)):
        days = _literal_days(sentence)
        starts = [0]
        for left, right in zip(days, days[1:]):
            if not re.fullmatch(r"[、,，及和与]*", sentence[left[1]:right[0]]):
                starts.append(right[0])
        for begin, end in zip(starts, [*starts[1:], len(sentence)]):
            yield sentence[begin:end]


def _source_relation(original, predicted):
    if (original["source_number"], original["page"]) != (predicted["source_number"], predicted["page"]):
        return False
    left, right = compact(original.get("text", "")), compact(predicted.get("raw_text", predicted.get("raw", "")))
    if not left or not right or not (left in right or right in left):
        return False
    if "_mapping" in original or "_mapping" in predicted:
        a, b = original.get("_mapping", {}), predicted.get("_mapping", {})
        return (a.get("status") == b.get("status") == "MATCHED" and spans_overlap(a["spans"], b["spans"]))
    # Pure synthetic callers can supply explicit original quotes and region
    # identities. The real CLI always provides a frozen SourceMapper instead.
    a, b = original.get("region_numbers"), predicted.get("region_numbers")
    return bool(a and b and set(a) & set(b))


def _event_proofs(left, right):
    if left["kind"] != right["content"]["kind"]:
        return []
    matched = []
    for mention in left["mentions"]:
        original = mention["source"]
        for proof in right["sources"]:
            if not _source_relation(original, proof):
                continue
            raw = proof.get("raw_text", "")
            units = list(_assertion_units(raw))
            # Exact, single-assertion identity also preserves unknown fields.
            # An entire multi-date paragraph must establish this event's own
            # original date/regimen association, not just share an OCR region.
            exact = compact(raw) == compact(original.get("text", "")) and len(units) <= 1
            variants = [normalized(value) for value in left.get("regimen_variants", [left.get("regimen")]) if value]
            linked = [unit for unit in units if (not left.get("date") or left["date"] in {day for _, _, day in _literal_days(unit)})
                      and (not variants or any(value in normalized(unit) for value in variants))]
            if exact or linked:
                matched.append(proof)
    return matched


def _field_supported(right, proofs, *, name):
    content = right["content"]
    for proof in proofs:
        units = list(_assertion_units(proof.get("raw_text", "")))
        dated_units = sum(bool(_literal_days(unit)) for unit in units)
        for unit in units:
            days = {day for _, _, day in _literal_days(unit)}
            if name != "regimen_texts" and content["date"] in days:
                return True
            if name == "regimen_texts" and normalized(content.get("regimen_text")) in normalized(unit):
                # A dated claim cannot borrow a scheme from another assertion.
                # Undated regimen identity remains independently measurable.
                if dated_units <= 1 or not content.get("date") or content["date"] in days:
                    return True
    return False


def _maximum_assignment(edges):
    """Deterministic maximum one-to-one matching, not a greedy value shortcut."""
    assigned = {}

    def claim(left, seen):
        for right in edges.get(left, []):
            if right in seen:
                continue
            seen.add(right)
            if right not in assigned or claim(assigned[right], seen):
                assigned[right] = left
                return True
        return False

    for left in sorted(edges):
        claim(left, set())
    return {left: right for right, left in assigned.items()}


def _component(expected, actual, pages, *, name):
    if name != "regimen_texts":
        actual = [row for row in actual if row["content"].get("date") and row["content"].get("date_precision") == "DAY"]
    else:
        actual = [row for row in actual if normalized(row["content"].get("regimen_text"))]
    def eligible(row):
        case = row["case"]
        if name == "reported_event_dates":
            return case.get("event_day_judgable", False)
        if name == "literal_dates":
            return case.get("literal_date_judgable", case.get("event_day_judgable", False))
        return case.get("regimen_identity_judgable", not row.get("regimen_conflict", False)) and bool(row.get("regimen"))

    def same_value(left, right):
        content = right["content"]
        day = content["date"] == left["date"] and content["date_precision"] == left["date_precision"]
        if name != "regimen_texts":
            return day
        variants = left.get("regimen_variants") or [left.get("regimen")]
        return normalized(content.get("regimen_text")) in {normalized(value) for value in variants if value}

    targets = [row for row in expected if eligible(row)]
    unjudged = [row for row in expected if not eligible(row)]
    proofs = {(i, j): _event_proofs(left, right) for i, left in enumerate(targets) for j, right in enumerate(actual)}
    edges = {i: [j for j, right in enumerate(actual) if proofs[i, j] and left["patient_group"] == right["patient_group"]
                  and same_value(left, right) and _field_supported(right, proofs[i, j], name=name)]
             for i, left in enumerate(targets)}
    matches = {i: (j, True) for i, j in _maximum_assignment(edges).items()}
    used = {j for j, _correct in matches.values()}
    reserved = {j for j, row in enumerate(actual) if j not in used and any(
        left["patient_group"] == row["patient_group"] and _event_proofs(left, row) for left in unjudged)}
    wrong_edges = {i: [j for j in range(len(actual)) if proofs[i, j] and j not in used and j not in reserved]
                   for i in range(len(targets)) if i not in matches}
    for i, j in _maximum_assignment(wrong_edges).items():
        matches[i] = (j, False)
        used.add(j)
    tp = sum(correct for _j, correct in matches.values())
    fp = sum(not correct for _j, correct in matches.values())
    fn = len(targets) - tp
    error_ids = {(actual[j]["patient_group"], actual[j]["id"]) for j, correct in matches.values() if not correct}
    trace = [{"gold_id": targets[i]["id"], "prediction_id": actual[j]["id"], "correct": correct}
             for i, (j, correct) in matches.items()]
    unknown = len(reserved)
    trace.extend({"prediction_id": actual[j]["id"], "correct": None, "reason": "source_identity_proved_component_unjudged"} for j in sorted(reserved))
    for j, row in enumerate(actual):
        if j in used or j in reserved:
            continue
        # Only explicit known source support is negative evidence. OCR-only or
        # partially annotated pages cannot silently become blanket negatives.
        duplicate_or_wrong = any(proofs[i, j] for i in range(len(targets)))
        crossed = any(location in pages and pages[location]["patient_group"] != row["patient_group"] for location in _locations(row))
        negative = any(pages.get(location, {}).get("review_state") == "ORIGINAL_AND_OCR_REVIEWED"
                       and pages[location]["relevance"] in NEGATIVE_SCOPES for location in _locations(row))
        if duplicate_or_wrong or crossed or negative:
            fp += 1
            error_ids.add((row["patient_group"], row["id"]))
            trace.append({"prediction_id": row["id"], "correct": False, "reason": "duplicate_or_unsupported_source_assertion"})
        else:
            unknown += 1
            trace.append({"prediction_id": row["id"], "correct": None, "reason": "original_component_not_judged"})
    return counts(tp, fp, fn, unknown), error_ids, trace


def _ordinal_words(raw):
    found = [int(value) for value in re.findall(r"(?<![A-Za-z0-9])C\s*(\d+)(?!\d)", raw, re.I)]
    for value in re.findall(r"第\s*([0-9零〇一二两三四五六七八九十百千]+)\s*(?:周期|疗程)", compact(raw)):
        if value.isascii() and value.isdigit():
            found.append(int(value))
            continue
        digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
        total, current = 0, 0
        for character in value:
            if character in digits:
                current = digits[character]
            else:
                total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[character]
                current = 0
        found.append(total + current)
    return set(found)


def _label_sources(target, pages):
    return [row["source"] for location in target["source_locations"]
            for row in pages.get((location["source_number"], location["page"]), {}).get("explicit_cycle_labels", [])
            if row.get("ordinal") == target["cycle_ordinal"]]


def _event_signature(event):
    sources = [{"source_number": proof["source_number"], "page": proof["page"],
                "quote": text_hash(compact(proof.get("raw_text", ""))),
                "spans": proof.get("_mapping", proof.get("mapping", {})).get("spans", [])}
               for proof in event.get("sources", [])]
    return json.dumps({"content": event["content"], "sources": sorted(sources, key=lambda row: json.dumps(row, sort_keys=True))},
                      sort_keys=True, ensure_ascii=False)


def score_predictions(gold, predictions, *, source_mapper=None):
    gold, predictions = deepcopy(gold), deepcopy(predictions)
    expected_files = {row["source_number"] for row in gold["sources"]}
    actual_files = [row["source_number"] for row in predictions["files"]]
    if set(actual_files) != expected_files or len(set(actual_files)) != len(actual_files):
        raise ValueError("Every original input, including failures, must appear once")
    if gold.get("cycles"):
        raise ValueError("This frozen unknown-boundary protocol requires independent review before scoring new joint positives")
    pages = {(source["source_number"], page["page"]): page for source in gold["sources"] for page in source["pages"]}
    mapping_trace = []
    if source_mapper is not None:
        for page in pages.values():
            for mention in [*page.get("events", []), *page.get("explicit_cycle_labels", [])]:
                proof = mention["source"]
                proof["_mapping"] = source_mapper.map_proof(proof, gold=True)
                mapping_trace.append({"owner": "gold", "mention_id": mention.get("mention_id"), "receipt": proof["_mapping"]})
        for group in predictions["groups"]:
            for event in group["events"]:
                for proof in event["sources"]:
                    proof["_mapping"] = source_mapper.map_proof(proof)
                    mapping_trace.append({"owner": "prediction_event", "id": event["id"], "receipt": proof["_mapping"]})
            for label in group["labels"]:
                label["_mapping"] = source_mapper.map_proof(label)
                mapping_trace.append({"owner": "prediction_label", "receipt": label["_mapping"]})
    expected = _gold_events(gold)
    events = [{**deepcopy(row), "patient_group": group["patient_group"]} for group in predictions["groups"] for row in group["events"]
              if row["content"]["kind"] in ANCHOR_KINDS and row["content"]["occurrence"] == "OCCURRED"]
    report, traces, errors = {}, {"source_mapping": mapping_trace}, set()
    for name in ("reported_event_dates", "literal_dates", "regimen_texts"):
        report[name], wrong, traces[name] = _component(expected, events, pages, name=name)
        errors.update(wrong)
    labels = [{**deepcopy(row), "patient_group": group["patient_group"]} for group in predictions["groups"] for row in group["labels"]
              if row.get("ordinal") and not row.get("reason")]
    gold_labels = [row for row in gold.get("unlinked_cycle_labels", []) if row.get("original_ordinal_judgable")]
    label_matches = {(i, j): any(_source_relation(proof, row) for proof in _label_sources(target, pages))
                     for i, target in enumerate(gold_labels) for j, row in enumerate(labels)}
    edges = {i: [j for j, row in enumerate(labels) if label_matches[i, j] and row["patient_group"] == target["patient_group"]
                 and row["ordinal"] == target["cycle_ordinal"] and row["ordinal"] in _ordinal_words(row.get("raw", ""))]
             for i, target in enumerate(gold_labels)}
    matched = _maximum_assignment(edges)
    used, ordinal_errors, ordinal_tp, ordinal_unknown, date_links_wrong = set(matched.values()), 0, len(matched), 0, 0
    wrong_links = []
    for i, j in matched.items():
        if labels[j].get("event_date") is not None and not gold_labels[i].get("ordinal_to_event_date_judgable"):
            date_links_wrong += 1
            wrong_links.append(labels[j])
    for i, row in enumerate(labels):
        if i in used:
            continue
        page = pages.get((row["source_number"], row["page"]), {})
        if any(label_matches[k, i] for k in range(len(gold_labels))) or (page and page["patient_group"] != row["patient_group"]):
            ordinal_errors += 1
        elif page.get("review_state") == "ORIGINAL_AND_OCR_REVIEWED" and page.get("relevance") in NEGATIVE_SCOPES:
            ordinal_errors += 1
        else:
            ordinal_unknown += 1
    report["original_ordinals"] = counts(ordinal_tp, ordinal_errors, len(gold_labels) - ordinal_tp, ordinal_unknown)
    report["ordinal_date_links"] = counts(fp=date_links_wrong)
    joint_wrong, joint_unknown, seen = 0, 0, set()
    traces["joint"] = []
    for group in predictions["groups"]:
        group_events = {row["id"]: row for row in group["events"]}
        for cycle in group["cycles"]:
            content = cycle["content"]
            identity = (group["patient_group"], tuple(sorted({_event_signature(group_events[key]) for key in cycle["event_ids"]})),
                        json.dumps(content, ensure_ascii=False, sort_keys=True))
            duplicate = identity in seen
            seen.add(identity)
            wrong = bool(duplicate or {(group["patient_group"], key) for key in cycle["event_ids"]} & errors
                         or content.get("true_d1_claimed") or content.get("end"))
            if content.get("ordinal") and content.get("anchor") and date_links_wrong:
                wrong = wrong or any(label["patient_group"] == group["patient_group"] and label.get("event_date") == content["anchor"]
                                     and label["ordinal"] == content["ordinal"] for label in wrong_links)
            joint_wrong += wrong
            joint_unknown += not wrong
            traces["joint"].append({"patient_group": group["patient_group"], "id": cycle["id"], "correct": False if wrong else None,
                                    "reason": "duplicate_cycle_assertion" if duplicate else "known_source_assertion_error" if wrong else "cycle_organization_unjudged"})
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


def predict_sources(manifest, patient_groups, dictionary, *, progress=None, source_mapper=None):
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
        source_texts = {source["id"]: source["text"] for source in inputs["sources"]}
        for event in proposals["events"]:
            for proof in event["sources"]:
                proof["source_context"] = source_texts[proof["source_id"]]
                if source_mapper is not None:
                    proof["mapping"] = source_mapper.map_proof(proof)
        for label in proposals["labels"]:
            label["source_number"], label["page"] = source_ids[label["source_id"]]
            label["source_context"] = source_texts[label["source_id"]]
            if source_mapper is not None:
                label["mapping"] = source_mapper.map_proof(label)
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
    source_mapper = SourceMapper.from_manifest(manifest)
    paths = sorted([*root.glob("apps/**/*.py"), root / "tools/phase_two_evaluation.py",
                    root / "tools/treatment_source_mapping.py", Path(__file__).resolve()])
    source_identity = {path.relative_to(root).as_posix(): file_digest(path) for path in paths}
    identity = {"frozen_at": datetime.now(timezone.utc).isoformat(), "prediction_seen": False,
                "manifest_sha256": args.manifest_sha256, "gold_sha256": args.gold_sha256, "protocol_sha256": args.protocol_sha256,
                "source_file_count": len(original), "page_count": sum(row["ocr_pages"] for row in original.values()),
                "original_and_ocr_hashes_verified": 2 * len(original), "patient_identity_groups": len(set(patient_groups.values())),
                "application_sources": source_identity, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                "dictionary_version": dictionary.version, "dictionary_hash": dictionary.content_hash,
                "source_mapping_version": MAPPING_VERSION}
    output.mkdir(parents=True)
    write = lambda name, value: (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    write("input-freeze.json", identity)
    predictions, execution = predict_sources(manifest, patient_groups, dictionary, source_mapper=source_mapper,
        progress=lambda done, total, status: print(f"{done}/{total} {status}", flush=True))
    write("predictions.json", predictions)
    report, traces = score_predictions(gold, predictions, source_mapper=source_mapper)
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
