"""Original-only relation candidate/source evaluation, separate from clinical identity."""

from collections import Counter, defaultdict
from itertools import combinations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unicodedata


EVALUATOR_VERSION = "lesion-original-clause-v1"
LABELS = {"INELIGIBLE_SAME_REPORT", "INCOMPATIBLE_PATIENT_BOUNDARY", "INCOMPATIBLE_EXPLICIT_ANATOMY",
          "SAME_ENTITY_EXPLICIT", "UNJUDGED_WHETHER_SAME_ENTITY"}
IDENTITY_KEYS = {"gold_sha256", "protocol_sha256", "mapper_sha256", "application_sha256", "manifest_sha256"}
REASON_FIELDS = {"explicit_location_equal": {"lesion.site"}, "explicit_side_equal": {"lesion.laterality"},
                 "explicit_body_equal": {"imaging.body_site"},
                 "reference_date_equal": {"comparison.reference_date", "report.exam_date"}}


def _normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).split())


def _value(value):
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_value(item) for item in value]
    return _normalized(value) if isinstance(value, str) else value


def _unique(rows, key):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Duplicate evaluation identity")
    return result


def _pair_key(first, second):
    return tuple(sorted((first, second)))


def _pages(report):
    return {page for first, last in report["page_ranges"] for page in range(first, last + 1)}


def _validate(gold, prediction):
    reports, nodes = _unique(gold["reports"], "id"), _unique(gold["nodes"], "id")
    sources = {}
    for report in reports.values():
        identity = {key: report[key] for key in ("source_number", "source_sha256", "ocr_sha256", "patient_group")}
        if report["source_number"] in sources and sources[report["source_number"]] != identity:
            raise ValueError("Inconsistent original report source identity")
        sources[report["source_number"]] = identity
    actual_sources = _unique(prediction["sources"], "source_number")
    if set(sources) != set(actual_sources) or any(
            any(actual_sources[number].get(key) != value for key, value in identity.items())
            for number, identity in sources.items()):
        raise ValueError("Original/OCR or approved patient group differs")
    for node in nodes.values():
        report = reports.get(node["report_id"])
        if report is None or any(node[key] != report[key] for key in ("source_number", "patient_group")):
            raise ValueError("Gold node belongs to another report or patient")
    pairs = {_pair_key(row["left_id"], row["right_id"]): row["label"] for row in gold["pairs"]}
    if (len(pairs) != len(gold["pairs"]) or set(pairs) != {_pair_key(*pair) for pair in combinations(nodes, 2)}
            or set(pairs.values()) - LABELS):
        raise ValueError("The complete original pair ledger must be retained")
    observations = _unique(prediction["observations"], "id")
    _unique(prediction["proposals"], "id")
    for row in observations.values():
        if row["source_number"] not in sources or row["patient_group"] != sources[row["source_number"]]["patient_group"]:
            raise ValueError("Observation patient/source differs from approved grouping")
    return reports, nodes, pairs, observations


def _field_proof(expected, actual):
    """Require the complete original clause; never match only an organ or number."""
    if (expected["field_key"] != actual["field_key"] or actual.get("source_valid") is not True
            or _value(expected["value"]) != _value(actual["content"]["value"])):
        return False
    sources, fragments = expected.get("sources", []), actual.get("fragments", [])
    if not sources or not fragments or {row["page"] for row in sources} != {row["page"] for row in fragments}:
        return False
    for page in {row["page"] for row in sources}:
        original = [row for row in sources if row["page"] == page]
        actual_page = [row for row in fragments if row["page"] == page]
        quote = _normalized("".join(row.get("raw_quote", "") for row in original))
        own = _normalized("".join(row.get("raw_text", "") for row in actual_page))
        if not quote or quote != own:
            return False
        for source in original:
            offsets, polygon = source.get("ocr_offsets"), source.get("exact_polygon")
            if offsets is not None:
                if not isinstance(offsets, dict) or set(offsets) != {"reading_order", "start_offset", "end_offset"}:
                    return False
                if not any(all(fragment.get(key) == value for key, value in offsets.items()) for fragment in actual_page):
                    return False
            if polygon is not None and not any(fragment.get("polygon") == polygon for fragment in actual_page):
                return False
    return True


def _mappings(reports, nodes, observations):
    edges, reverse, in_scope = defaultdict(set), defaultdict(set), {}
    for row in observations.values():
        page_set = set(row["report_pages"])
        eligible_reports = {report["id"] for report in reports.values()
                            if row["source_number"] == report["source_number"] and page_set and page_set <= _pages(report)}
        in_scope[row["id"]] = bool(eligible_reports)
        if row.get("report_source_valid") is not True:
            continue
        sites = [field for field in row["fields"] if field["field_key"] == "lesion.site"]
        for node in nodes.values():
            if node["report_id"] in eligible_reports and any(_field_proof(node["local_identity_source"], field) for field in sites):
                edges[row["id"]].add(node["id"])
                reverse[node["id"]].add(row["id"])
    mapped, audit = {}, []
    for identity in observations:
        options = edges[identity]
        unique = len(options) == 1 and len(reverse[next(iter(options))]) == 1
        if unique:
            mapped[identity] = next(iter(options))
        status = "MAPPED" if unique else "OUTSIDE_SCOPE" if not in_scope[identity] else "AMBIGUOUS" if options else "UNMAPPED"
        audit.append({"observation_id": identity, "gold_node_id": mapped.get(identity), "status": status,
                      "original_clause_candidates": sorted(options)})
    return mapped, audit


def _reason_proof(reason, endpoints, gold_nodes, reports):
    required = REASON_FIELDS.get(reason.get("code"))
    if not required or not reason.get("field_ids") or len(set(reason["field_ids"])) != len(reason["field_ids"]):
        return False
    # Require each listed field from the actual two endpoints, each backed by its
    # own frozen field quote. A valid site cannot prove another field's origin.
    owners = defaultdict(list)
    for index, row in enumerate(endpoints):
        for field in row["fields"] + row["context_fields"]:
            owners[field["id"]].append((index, field))
    covered_endpoints, kinds, values = set(), set(), []
    for identity in reason["field_ids"]:
        matches = owners.get(identity, [])
        if len(matches) != 1:
            return False
        index, field = matches[0]
        if field["field_key"] not in required:
            return False
        node = gold_nodes[index]
        report = reports[node["report_id"]]
        expected = [node["local_identity_source"], *node.get("original_fields", []),
                    *report.get("original_context_fields", []), *report.get("comparison_fields", [])]
        if not any(_field_proof(candidate, field) for candidate in expected):
            return False
        covered_endpoints.add(index)
        kinds.add(field["field_key"])
        value = field["content"]["value"]
        if reason["code"] == "reference_date_equal":
            if value.get("precision") != "DAY":
                return False
            values.append(value.get("value"))
        elif reason["code"] == "explicit_side_equal":
            values.append(value.get("code"))
        else:
            values.append(value.get("text"))
    return covered_endpoints == {0, 1} and kinds == required and bool(values) and all(
        isinstance(value, str) and value and _normalized(value) == _normalized(values[0]) for value in values)


def _automatic_mutations(prediction):
    fields = [*prediction.get("all_fields", []),
              *(field for row in prediction["observations"] for field in row["fields"] + row["context_fields"])]
    non_pending = {field["id"] for field in fields if "status" in field and field["status"] != "PENDING"}
    # Count immutable events regardless of their resulting state. A reset or
    # undo back to PENDING/UNASSIGNED cannot erase an automatic user decision.
    # These are separate contract observations, not a count of unique actions.
    return {"stable_lesions": len(prediction["stable_lesions"]),
            "assignment_revisions": len(prediction["assignments"]),
            "non_pending_proposals": sum(row["status"] != "PENDING" for row in prediction["proposals"]),
            "proposal_decision_revisions": sum(row.get("action") != "PROPOSE" or row.get("status") != "PENDING"
                                                for row in prediction.get("proposal_revisions", [])),
            "non_pending_fields": len(non_pending), "fact_revisions": len(prediction.get("fact_revisions", []))}


def evaluate_relations(gold, prediction):
    reports, nodes, pairs, observations = _validate(gold, prediction)
    mapped, mapping_audit = _mappings(reports, nodes, observations)
    outside = {row["observation_id"] for row in mapping_audit if row["status"] == "OUTSIDE_SCOPE"}
    results, supported_pairs = [], set()
    for proposal in prediction["proposals"]:
        endpoints = [observations.get(proposal[key]) for key in ("first_id", "second_id")]
        if any(row is None for row in endpoints) or endpoints[0]["id"] == endpoints[1]["id"]:
            label, pair = "INVALID_ENDPOINT", None
        elif any(row["id"] in outside for row in endpoints):
            label, pair = "OUTSIDE_SCOPE_ENDPOINT", None
        elif not all(row["id"] in mapped for row in endpoints):
            label, pair = "UNMAPPED_ENDPOINT", None
        else:
            pair = _pair_key(*(mapped[row["id"]] for row in endpoints))
            label = pairs[pair]
        reason_audit = [{"code": reason.get("code"), "verified": bool(pair and _reason_proof(
            reason, endpoints, [nodes[mapped[row["id"]]] for row in endpoints], reports))}
            for reason in proposal.get("reasons", [])]
        source_verified = (any(row["code"] == "explicit_location_equal" for row in reason_audit)
                           and all(row["verified"] for row in reason_audit))
        if label == "SAME_ENTITY_EXPLICIT" and source_verified:
            supported_pairs.add(pair)
        results.append({"proposal_id": proposal["id"], "label": label, "gold_pair": pair,
                        "source_verified": source_verified, "reasons": reason_audit, "status": proposal["status"]})
    labels = Counter(row["label"] for row in results)
    statuses = Counter(row["status"] for row in results)
    positive = sum(label == "SAME_ENTITY_EXPLICIT" for label in pairs.values())
    judged_predictions = sum(row["label"] == "SAME_ENTITY_EXPLICIT" or row["label"].startswith("INCOMPATIBLE_")
                             for row in results)
    precision = len(supported_pairs) / judged_predictions if positive and judged_predictions else None
    recall = len(supported_pairs) / positive if positive else None
    metrics = {"precision": precision, "recall": recall,
               "f1": (2 * precision * recall / (precision + recall) if precision + recall else 0.0) if precision is not None else None,
               "positive_gold": positive}
    # Unjudged relations are neither positives nor negatives. The separate full
    # label/coverage ledger retains every unmapped and unjudged candidate; precision
    # uses only originally adjudicated identities and is null without positives.
    mutations = _automatic_mutations(prediction)
    score = {"observations": {"gold": len(nodes), "predicted": len(observations), "mapped": len(mapped),
                             "missing": len(nodes) - len(set(mapped.values())), "unmapped": len(observations) - len(mapped),
                             "outside_scope": len(outside)},
             "pair_universe": {"total": len(pairs), "by_label": dict(Counter(pairs.values()))},
             "proposals": {"total": len(results), "by_label": dict(labels), "by_status": dict(statuses),
                           "incompatible": sum(count for label, count in labels.items() if label.startswith("INCOMPATIBLE_"))},
             "reason_sources": {"verified": sum(row["source_verified"] for row in results),
                                "unverified": sum(not row["source_verified"] for row in results)},
             "identity_candidate_metrics": metrics,
             "identity_candidate_scored_predictions": judged_predictions,
             "automatic_mutations": mutations, "automatic_confirmation_failures": sum(mutations.values()),
             "mutation_evidence": {key: key in prediction for key in ("all_fields", "fact_revisions", "proposal_revisions")}}
    return score, {"mappings": mapping_audit, "proposals": results}


def validate_execution_approval(approval, identities):
    if (set(identities) != IDENTITY_KEYS or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in identities.values())
            or approval.get("status") != "APPROVED_FIRST_RELATION_EXECUTION"
            or approval.get("relation_prediction_authorized") is not True or approval.get("identities") != identities):
        raise ValueError("Independent approval for these exact relation execution identities is required")


def predict_relations(manifest, original_groups, *, dictionary=None, progress=None):
    """Frozen OCR to actual persistence and pending proposals; no gold labels passed."""
    from django.db import connection
    from apps.documents.models import Document
    from apps.facts.clinical_readmodels import report_material
    from apps.facts.models import FactRevision
    from apps.labs.dictionary import current_dictionary
    from apps.lesions.models import Lesion, LesionObservationRevision, LesionProposalRevision
    from apps.lesions.readmodels import observation_material, proposal_material
    from apps.lesions.services import generate_proposals
    from apps.processing.models import ParsingVersion
    from tools.phase_two_evaluation import predict_frozen_sources

    if (connection.vendor != "sqlite" or "memory" not in str(connection.settings_dict["NAME"])
            or Document.objects.exists() or Lesion.objects.exists()):
        raise ValueError("Relation replay requires an empty ephemeral in-memory database")
    sources = _unique(manifest["sources"], "source_number")
    if set(sources) != set(original_groups) or any(not isinstance(group, str) or not group for group in original_groups.values()):
        raise ValueError("Every source needs its independently reviewed patient group")
    cache_roots = {Path(source["ocr_path"]).parent for source in sources.values()}
    if len(cache_roots) != 1:
        raise ValueError("Frozen OCR inputs must use one private cache directory")
    baseline = []
    for source in sources.values():
        for path_key, hash_key in (("source_path", "source_sha256"), ("ocr_path", "ocr_sha256")):
            if hashlib.sha256(Path(source[path_key]).read_bytes()).hexdigest() != source[hash_key]:
                raise ValueError("Frozen original or OCR bytes changed before replay")
        cache = json.loads(Path(source["ocr_path"]).read_text(encoding="utf-8"))
        baseline.append({"source_file_hash": source["source_sha256"], "ocr_cache_sha256": source["ocr_sha256"],
                         "ocr_pages": len(cache["pages"])})
    dictionary = dictionary or current_dictionary()
    replay = predict_frozen_sources(
        [{"hash": source["source_sha256"], "path": source["source_path"]} for source in sources.values()],
        next(iter(cache_roots)), {"samples": baseline}, {"files": [
            {"source_file_hash": source["source_sha256"], "patient_group_id": original_groups[number]}
            for number, source in sources.items()]}, dictionary, progress=progress)
    versions = set(ParsingVersion.objects.values_list("dictionary_version", "dictionary_hash"))
    if versions and versions != {(dictionary.version, dictionary.content_hash)}:
        raise ValueError("Persisted pipeline dictionary differs from the selected artifact")
    documents, patients, extraction_states = {}, {}, {}
    for number, source in sources.items():
        document = Document.objects.select_related("patient__account").get(sha256=source["source_sha256"])
        documents[str(document.pk)] = source
        version = document.parsing_versions.filter(active=True).first()
        extraction = getattr(version, "clinical_extraction", None) if version else None
        extraction_states[number] = {"clinical_status": extraction.status if extraction else "FAILED",
            "unparsed_pages": extraction.unparsed_page_count if extraction else document.page_count}
        group = original_groups[number]
        if group in patients and patients[group].pk != document.patient_id:
            raise ValueError("Approved patient group was split by the replay")
        patients[group] = document.patient
    if len({patient.pk for patient in patients.values()}) != len(patients):
        raise ValueError("Different original patient groups were combined")
    result = {"sources": [{**{key: source[key] for key in ("source_number", "source_sha256", "ocr_sha256")},
                            "patient_group": original_groups[number], **extraction_states[number]}
                           for number, source in sources.items()],
              "observations": [], "proposals": [], "stable_lesions": [], "assignments": []}
    for group, patient in patients.items():
        # Use production service with the isolated container's actual actor.
        # It can create proposals, never user field confirmations or lesion IDs.
        generate_proposals(patient, actor=patient.account)
        reports = {row["id"]: row for row in report_material(patient, include_history=True)}
        for row in observation_material(patient, include_unavailable=True):
            source = documents[row["document_id"]]
            report = reports[row["report_id"]]
            result["observations"].append({**row, "source_number": source["source_number"], "patient_group": group,
                "report_pages": report["pages"], "report_source_valid": report["source_valid"] and report["status"] == "ACTIVE"})
        result["proposals"].extend(proposal_material(patient, include_history=True))
    result["stable_lesions"] = list(Lesion.objects.values("pk"))
    result["assignments"] = list(LesionObservationRevision.objects.values_list("after", flat=True))
    # Audit the final database state, including fields outside local observations
    # and any decision that was later undone back to its initial state.
    result["all_fields"] = [{key: field[key] for key in ("id", "report_id", "status", "revision_number")}
        for patient in patients.values() for report in report_material(patient, include_history=True)
        for field in report["fields"]]
    result["fact_revisions"] = [{"id": str(row.pk), "fact_id": str(row.fact_id), "sequence": row.sequence,
                                 "status": row.after.get("status")}
                                for row in FactRevision.objects.order_by("fact_id", "sequence")]
    result["proposal_revisions"] = [{"id": str(row.pk), "proposal_id": str(row.proposal_id), "sequence": row.sequence,
                                      "status": row.after.get("status"), "action": row.operation.action}
        for row in LesionProposalRevision.objects.select_related("operation").order_by("proposal_id", "sequence")]
    execution = {**replay["execution"], "original_patient_groups": len(patients),
                 "dictionary_version": dictionary.version, "dictionary_hash": dictionary.content_hash,
                 "field_status_counts": dict(Counter(field["status"] for field in result["all_fields"])),
                 "field_revision_count": len(result["fact_revisions"])}
    return json.loads(json.dumps(result, ensure_ascii=False, default=str)), execution


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_identity(root):
    """Actual bytes, including new implementation files; never private runtime data."""
    names = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
    selected = {name for name in names if name.startswith(("apps/", "config/", "templates/", "static/", "tools/"))
                or name in {"pyproject.toml", "package.json", "package-lock.json"}}
    return {name: _hash(root / name) for name in sorted(selected) if (root / name).is_file()}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_bytes(path, content):
    with path.open("xb") as handle:
        handle.write(content)


def _input_groups(gold, manifest):
    declared = {}
    for report in gold["reports"]:
        identity = {key: report[key] for key in ("source_number", "source_sha256", "ocr_sha256", "patient_group")}
        if report["source_number"] in declared and declared[report["source_number"]] != identity:
            raise ValueError("Original report patient/source identity differs")
        declared[report["source_number"]] = identity
    sources = _unique(manifest["sources"], "source_number")
    if set(sources) != set(declared):
        raise ValueError("Complete source scope differs from frozen gold")
    for number, source in sources.items():
        for path_key, hash_key in (("source_path", "source_sha256"), ("ocr_path", "ocr_sha256")):
            if source[hash_key] != declared[number][hash_key] or _hash(source[path_key]) != source[hash_key]:
                raise ValueError("Frozen original/OCR identity differs before execution")
    # Validate the complete gold ledger before any application execution. The
    # production pipeline receives only this independently reviewed grouping.
    _validate(gold, {"sources": list(declared.values()), "observations": [], "proposals": []})
    return {number: row["patient_group"] for number, row in declared.items()}


def main(argv=None):
    import argparse
    import os
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "gold", "protocol", "report"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--describe", action="store_true", help="Write an execution request without running the application")
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approval-sha256")
    parser.add_argument("--predictions", type=Path, help="Re-score retained predictions without application execution")
    parser.add_argument("--prediction-report", type=Path, help="Original generation report binding retained prediction bytes")
    args = parser.parse_args(argv)
    if args.report.exists() or (args.private_output and args.private_output.exists()):
        raise ValueError("Retained evidence is immutable; choose fresh output paths")
    if bool(args.predictions) != bool(args.prediction_report):
        raise ValueError("Retained predictions and their original generation report are required together")
    if args.describe and (args.predictions or args.approval or args.private_output):
        raise ValueError("Description cannot also generate, re-score or approve an execution")
    if not args.describe and not args.predictions and (not args.approval or not args.approval_sha256):
        raise ValueError("Independent execution approval and its SHA-256 are required before database initialization")
    if not args.describe and args.private_output is None:
        raise ValueError("A fresh private output directory is required")
    if _hash(args.gold) != args.gold_sha256:
        raise ValueError("Frozen original-only gold bytes changed")
    gold, manifest = _json(args.gold), _json(args.manifest)
    if (not gold["policy"]["status"].startswith("FROZEN")
            or gold["policy"].get("real_relation_predictions_run") is not False
            or gold["policy"]["protocol_sha256"] != _hash(args.protocol)):
        raise ValueError("Original-only gold and its unchanged frozen protocol are required")
    groups = _input_groups(gold, manifest)
    files = source_identity(root)
    identities = {"gold_sha256": args.gold_sha256, "protocol_sha256": _hash(args.protocol),
                  "manifest_sha256": _hash(args.manifest), "mapper_sha256": _hash(__file__),
                  "application_sha256": _canonical_hash(files)}
    if args.describe:
        _write_json(args.report, {"schema_version": 1, "status": "AWAITING_INDEPENDENT_EXECUTION_APPROVAL",
            "real_relation_predictions_run": False, "identities": identities, "source_files": files,
            "scope": {"sources": len(manifest["sources"]), "reports": len(gold["reports"]),
                      "original_patient_groups": len(set(groups.values())), "observations": len(gold["nodes"]),
                      "pairs": len(gold["pairs"]), "pair_labels": dict(Counter(row["label"] for row in gold["pairs"]))}})
        return 0
    original = original_bytes = prediction_bytes = None
    if args.predictions:
        original_bytes = args.prediction_report.read_bytes()
        prediction_bytes = args.predictions.read_bytes()
        original = json.loads(original_bytes)
        if (original.get("execution_kind") != "actual_pipeline_replay" or "current" not in original
                or any(original["identity"]["identities"][key] != identities[key]
                       for key in ("gold_sha256", "protocol_sha256", "manifest_sha256"))
                or original["identity"]["prediction_content_sha256"] != hashlib.sha256(prediction_bytes).hexdigest()):
            raise ValueError("Retained predictions differ from their original execution and frozen inputs")
        prediction = json.loads(prediction_bytes)
        execution = original["execution"]
    else:
        if _hash(args.approval) != args.approval_sha256:
            raise ValueError("Independent execution approval bytes changed")
        validate_execution_approval(_json(args.approval), identities)
    # Capture bytes before execution; any later mutation invalidates the result.
    # This fresh directory is owned by this attempt. Never append failure data
    # to an existing output directory or replace a prior generation's artifacts.
    args.private_output.mkdir(parents=True, exist_ok=False)
    kind = "retained_prediction_rescore" if original else "actual_pipeline_replay"
    identity = {"identities": identities, "source_files": original["identity"]["source_files"] if original else files,
                "generation_identities": original["identity"]["identities"] if original else identities,
                "approval_sha256": original["identity"]["approval_sha256"] if original else args.approval_sha256,
                "original_generation_report_sha256": hashlib.sha256(original_bytes).hexdigest() if original else None}

    def validate_current():
        if files != source_identity(root):
            raise ValueError("Source files changed during execution; no result can be published")
        _input_groups(gold, manifest)
        if any(_hash(path) != identities[key] for path, key in (
                (args.gold, "gold_sha256"), (args.protocol, "protocol_sha256"), (args.manifest, "manifest_sha256"))):
            raise ValueError("Evaluation inputs changed during execution")
        if original and (_hash(args.predictions) != identity["prediction_content_sha256"]
                         or _hash(args.prediction_report) != identity["original_generation_report_sha256"]):
            raise ValueError("Retained re-score inputs changed during execution")

    stage, captured, generation_sha = "SOURCE_SNAPSHOT", False, None
    try:
        for name in ["tools/lesion_relation_evaluation.py"] if original else files:
            target = args.private_output / ("scorer_snapshot" if original else "source_snapshot") / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(target, (root / name).read_bytes())
        stage = "PREDICTION"
        if not original:
            os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
            import django
            django.setup()
            from django.core.management import call_command
            from django.db import connection
            if (connection.vendor != "sqlite" or str(connection.settings_dict["NAME"]) != ":memory:"
                    or connection.introspection.table_names()):
                raise ValueError("CLI requires a new process with an empty ephemeral in-memory database")
            call_command("migrate", verbosity=0)
            prediction, execution = predict_relations(manifest, groups,
                progress=lambda done, total, status: print(f"{done}/{total} {status}", flush=True))
        stage = "PREDICTION_CAPTURE"
        # Save completed raw output before any scorer or post-generation guard.
        # The immutable receipt is explicitly unscored, not a quality pass.
        if original:
            _write_bytes(args.private_output / "predictions.json", prediction_bytes)
        else:
            _write_json(args.private_output / "predictions.json", prediction)
        identity["prediction_content_sha256"] = _hash(args.private_output / "predictions.json")
        captured = True
        _write_json(args.private_output / "generation.json", {"schema_version": 1,
            "status": "PREDICTIONS_CAPTURED_UNSCORED", "execution_kind": kind,
            "identity": identity, "execution": execution})
        generation_sha = _hash(args.private_output / "generation.json")
        if original:
            _write_bytes(args.private_output / "original-generation-report.json", original_bytes)
        stage = "POST_PREDICTION_VALIDATION"
        validate_current()
        stage = "SCORING"
        score, audit = evaluate_relations(gold, prediction)
        stage = "POST_SCORING_VALIDATION"
        validate_current()
        if _hash(args.private_output / "predictions.json") != identity["prediction_content_sha256"]:
            raise ValueError("Captured raw prediction bytes changed before score publication")
        stage = "SCORE_PUBLICATION"
        _write_json(args.private_output / "assignments.json", audit)
        report = {"schema_version": 1, "evaluator_version": EVALUATOR_VERSION,
            "status": "SCORED", "execution_kind": kind, "current": score,
            "files": {"total": len(prediction["sources"]),
                      "failed": sum(row.get("clinical_status") == "FAILED" for row in prediction["sources"]),
                      "unparsed_pages": sum(row.get("unparsed_pages", 0) for row in prediction["sources"])},
            "execution": execution,
            "identity": {**identity, "generation_receipt_sha256": generation_sha,
                         "assignment_content_sha256": _hash(args.private_output / "assignments.json")},
            "method": {"mapping": "Unique original complete normalized clause and page; ambiguous matches receive no credit",
                       "reason_sources": "Every actual endpoint reason field requires its own unchanged original quote or frozen locator",
                       "unjudged_pairs": "Additional review work, neither correct clinical identity nor false positive",
                       "clinical_identity_metrics_without_positive_gold": None,
                       "automatic_mutations": "Counts of forbidden rows, revisions and effective states; these are separate contract observations, not unique user actions",
                       "all_inputs_pairs_and_candidates_retained": True, "old_field_and_excerpt_gold_unchanged": True,
                       "development_set_not_held_out": True}}
        _write_json(args.report, report)
    except Exception as error:
        _write_json(args.private_output / "failure.json", {"schema_version": 1,
            "status": "FAILED", "stage": stage, "execution_kind": kind, "identity": identity,
            "prediction_captured": captured, "prediction_content_sha256": identity.get("prediction_content_sha256"),
            "generation_receipt_sha256": generation_sha, "exception_type": type(error).__name__})
        raise
    print(json.dumps(score, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
