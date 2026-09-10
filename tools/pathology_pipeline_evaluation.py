"""Run fixed OCR through real persistence, then map and separately score fields.

Use only an independently approved exact command and a new ignored output
directory. This is a development replay, not OCR accuracy or clinical accuracy.
The prediction function never receives gold, annotation scope or expected values.
Original text, candidates and matching traces are private local artifacts.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


VERSION = "PATHOLOGY_FIXED_PIPELINE_V1"


class EvaluationInputError(ValueError):
    pass


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_manifest(manifest, *, expected_counts=None):
    """Recheck every source/cache byte and raw region, before any app write.

    Explicit execution counts prevent a shortened manifest from claiming the
    approved complete denominator. The CLI requires all three counts; synthetic
    callable tests may omit them. Original array order is never sorted here.
    """
    from tools.pathology_source_mapping import MappingInputError, original_pages

    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list) or not sources:
        raise EvaluationInputError("Explicit complete source inventory is required")
    numbers, identities, paths, roots, raw, originals = set(), set(), set(), set(), {}, []
    try:
        for source in sources:
            number, identity, count = source.get("source_number"), source.get("source_sha256"), source.get("ocr_pages")
            if (type(number) is not int or number < 1 or number in numbers or identity in identities
                    or not isinstance(identity, str) or not re.fullmatch("[0-9a-f]{64}", identity)
                    or type(count) is not int or count < 1):
                raise EvaluationInputError("Repeated or invalid original source/page identity")
            numbers.add(number)
            identities.add(identity)
            encoded = None
            for path_key, hash_key in (("source_path", "source_sha256"), ("ocr_path", "ocr_sha256")):
                expected = source.get(hash_key)
                path = Path(source[path_key]).resolve(strict=True)
                if path in paths or not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
                    raise EvaluationInputError("Original/cache file identity is repeated or invalid")
                paths.add(path)
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != expected:
                    raise EvaluationInputError("Original or fixed OCR bytes changed")
                if path_key == "ocr_path":
                    if path.name != identity + ".json":
                        raise EvaluationInputError("OCR filename is not bound to the original SHA")
                    roots.add(path.parent)
                    encoded = content
            cache = json.loads(encoded.decode("utf-8"))
            pages = cache.get("pages")
            if (cache.get("source_file_hash") != identity or not isinstance(pages, list) or len(pages) != count
                    or [p.get("page_number") for p in pages] != list(range(1, count + 1))):
                raise EvaluationInputError("Fixed original page sequence or cache source changed")
            originals.extend(original_pages(pages, source_sha256=identity, ocr_sha256=source["ocr_sha256"]))
            raw[identity] = pages
    except (OSError, KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, MappingInputError) as error:
        raise EvaluationInputError("Original inventory cannot be verified") from error
    if len(roots) != 1:
        raise EvaluationInputError("Frozen OCR caches must share their verified input root")
    counts = (len(sources), len(originals), len(paths))
    if expected_counts is not None and (len(expected_counts) != 3 or any(type(n) is not int or n < 1 for n in expected_counts)
                                        or tuple(expected_counts) != counts):
        raise EvaluationInputError("Explicit approved source/page/file denominators changed")
    return raw, {"pages": originals}


def verify_coverage(manifest, coverage):
    expected = {(s["source_sha256"], s["ocr_sha256"], page) for s in manifest["sources"] for page in range(1, s["ocr_pages"] + 1)}
    actual = [(row.get("source_sha256"), row.get("ocr_sha256"), row.get("page")) for row in coverage]
    if len(actual) != len(expected) or set(actual) != expected or any(type(row[2]) is not int for row in actual):
        raise EvaluationInputError("Gold coverage must represent every original page exactly once")


def verify_scoring_contract(protocol, contract, predicates, *, expected_counts, input_hashes, gold=None):
    """Bind executable components, annotation population and appendix identity.

    This checks the frozen contract, not prediction quality. Source proof and
    per-field outcomes remain exclusively the pure scorer's responsibility.
    """
    from tools.pathology_molecular_evaluation import COMPONENTS, STATES

    if (protocol.get("protocol_version") != "PATHOLOGY_IHC_SCOPED_SCORING_V1"
            or protocol.get("field_components") != list(COMPONENTS)
            or set(protocol.get("field_outcomes", {})) != set(STATES)
            or contract.get("version") != "PATHOLOGY_IHC_EVALUATION_CONTRACT_V1"
            or set(contract.get("components", {})) != set(COMPONENTS)
            or predicates.get("version") != "PATHOLOGY_IHC_NEGATIVE_PREDICATES_V1"
            or any(predicates.get(name + "_sha256") != input_hashes[name] for name in ("gold", "protocol"))):
        raise EvaluationInputError("Scorer, protocol, contract or negative appendix binding disagrees")
    population = protocol.get("population")
    if (not isinstance(population, dict) or any(type(population.get(key)) is not int or population[key] != count
                                             for key, count in zip(("inputs", "pages"), expected_counts))):
        raise EvaluationInputError("Protocol and approved execution population disagree")
    if gold is not None:
        fields, coverage = gold["fields"], gold["coverage"]
        actual = {"expected_fields": len(fields), "independent_score_fields": sum(f["field_key"] == "ihc.score" for f in fields),
                  "context_and_metadata_fields": sum(f["field_key"] != "ihc.score" for f in fields),
                  "negative_regions": len(gold["negative_boundaries"]),
                  "out_of_gold_pages": sum(p["state"] != "SCOPED_FIELDS_AND_EXCLUSIONS" for p in coverage),
                  "unreviewed_subset": sum(p["state"] == "UNREVIEWED_UNJUDGED" for p in coverage),
                  "scope_visual_only_subset": sum(p["state"] == "SCOPE_VISUAL_ONLY_NOT_IHC_GOLD" for p in coverage)}
        if any(type(population.get(key)) is not int or population[key] != count for key, count in actual.items()):
            raise EvaluationInputError("Frozen gold population differs from its scoring protocol")


def checked_output(root, output):
    output = Path(output).resolve()
    if not output.is_relative_to((Path(root) / ".runtime").resolve()) or output.exists():
        raise EvaluationInputError("A fresh private .runtime output directory is required")
    return output


def predict_sources(manifest, dictionary, *, expected_counts=None, progress=None):
    """Real fixed-provider persistence and projection; no evaluation answers."""
    from django.db import connection
    from apps.documents.models import Document
    from tools.pathology_source_mapping import map_document
    from tools.phase_two_evaluation import predict_frozen_sources

    if (connection.vendor != "sqlite" or str(connection.settings_dict["NAME"]) not in {
            ":memory:", "file:memorydb_default?mode=memory&cache=shared"} or Document.objects.exists()):
        raise EvaluationInputError("Use an empty isolated in-memory evaluation database")
    raw, originals = verify_manifest(manifest, expected_counts=expected_counts)
    sources = manifest["sources"]
    # The actual shared replay entry calls DocumentProcessingPipeline._persist,
    # including extract_clinical_version and its independent savepoint. Patient
    # grouping is one isolated source per synthetic patient, never gold labels.
    replay = predict_frozen_sources(
        [{"hash": s["source_sha256"], "path": s["source_path"]} for s in sources], Path(sources[0]["ocr_path"]).parent,
        {"samples": [{"source_file_hash": s["source_sha256"], "ocr_cache_sha256": s["ocr_sha256"], "ocr_pages": s["ocr_pages"]} for s in sources]},
        {"files": [{"source_file_hash": s["source_sha256"], "patient_group_id": s["source_sha256"]} for s in sources]},
        dictionary, progress=progress)
    statuses = {row["source_file_hash"]: row for row in replay["predictions"]}
    documents = {row.sha256: row for row in Document.objects.all()}
    if (set(statuses) != set(raw) or set(documents) != set(raw)
            or len(replay["predictions"]) != len(raw) or Document.objects.count() != len(raw)):
        raise EvaluationInputError("Actual persisted source inventory is incomplete or repeated")
    result = {"mapper_schema": 1, "pages": [], "document_receipts": []}
    for source in sources:
        identity = source["source_sha256"]
        status = statuses[identity]["status"]
        state = "COMPLETE" if status in {"success", "original_only"} else "FAILED"
        mapped = map_document(documents[identity].pk, raw[identity], source_sha256=identity, ocr_sha256=source["ocr_sha256"],
                              execution_status={page: state for page in range(1, source["ocr_pages"] + 1)})
        if mapped["receipt"]["parsing_version_id"] and (mapped["receipt"]["dictionary_version"] != dictionary.version
                                                        or mapped["receipt"]["dictionary_hash"] != dictionary.content_hash):
            raise EvaluationInputError("Actually persisted dictionary identity differs from the requested dictionary")
        result["pages"].extend(mapped["pages"])
        result["document_receipts"].append({"source_number": source["source_number"], "source_sha256": identity,
            "fixed_ocr_sha256": source["ocr_sha256"], "legacy_replay_status": status,
            "pipeline_failure_state": statuses[identity].get("failure_state"), **mapped["receipt"]})
    return result, originals, {**deepcopy(replay["execution"]), "runner_version": VERSION,
        "fixed_source_count": len(sources), "fixed_page_count": len(originals["pages"]), "verified_input_files": 2 * len(sources),
        "failed_pipeline_sources": sum(row["status"] == "failed" for row in statuses.values()),
        "clinical_failed_sources": sum(r["clinical_status"] == "FAILED" for r in result["document_receipts"]),
        "patient_grouping": "One isolated synthetic patient per original; no cross-source grouping inference."}


def application_identity(root):
    command = lambda *args: subprocess.check_output(["git", "-C", str(root), *args])
    if command("status", "--porcelain", "--untracked-files=normal").strip():
        raise EvaluationInputError("Commit and independently review the exact source before execution")
    entries = command("ls-files", "-s", "-z").decode("utf-8").split("\0")
    files = {}
    for entry in filter(None, entries):
        meta, name = entry.split("\t", 1)
        mode, blob, stage = meta.split()
        if stage != "0" or mode not in {"100644", "100755"}:
            raise EvaluationInputError("Unmerged or indirect tracked source cannot be frozen")
        files[name] = {"git_blob": blob, "worktree_sha256": file_digest(Path(root) / name)}
    return {"head": command("rev-parse", "HEAD").decode().strip(), "tracked_files": files}


def main(argv=None):
    import argparse
    import os
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    input_names = ("manifest", "gold", "protocol", "evidence", "contract", "negative_predicates")
    for name in input_names:
        option = name.replace("_", "-")
        parser.add_argument("--" + option, type=Path, required=True)
        parser.add_argument("--" + option + "-sha256", required=True)
    for name in ("source-count", "page-count", "input-file-count"):
        parser.add_argument("--" + name, type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--dictionary-version", required=True)
    parser.add_argument("--dictionary-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    def check_inputs():
        for name in input_names:
            if file_digest(getattr(args, name)) != getattr(args, name + "_sha256"):
                raise EvaluationInputError("Frozen evaluation input identity changed: " + name)

    check_inputs()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    counts = (args.source_count, args.page_count, args.input_file_count)
    input_hashes = {name: getattr(args, name + "_sha256") for name in input_names}
    protocol, contract, predicates = (json.loads(getattr(args, name).read_text(encoding="utf-8"))
                                      for name in ("protocol", "contract", "negative_predicates"))
    verify_scoring_contract(protocol, contract, predicates, expected_counts=counts, input_hashes=input_hashes)
    verify_manifest(manifest, expected_counts=counts)
    application = application_identity(root)
    if application["head"] != args.expected_head:
        raise EvaluationInputError("Reviewed application HEAD changed")
    output = checked_output(root, args.output)
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    import django
    django.setup()
    from django.core.management import call_command
    from apps.labs.dictionary import current_dictionary
    from tools.phase_two_evaluation import capture_parser_identity
    from tools.pathology_molecular_evaluation import evaluate

    call_command("migrate", verbosity=0)
    dictionary = current_dictionary()
    if dictionary.version != args.dictionary_version or dictionary.content_hash != args.dictionary_sha256:
        raise EvaluationInputError("Reviewed dictionary identity changed")
    parser_identity = capture_parser_identity(root)
    identity = {"runner_version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "prediction_seen": False,
        "inputs": input_hashes, "application": application,
        "parser": parser_identity, "dictionary": {"version": dictionary.version, "content_hash": dictionary.content_hash},
        "source_count": counts[0], "page_count": counts[1], "verified_input_files": counts[2]}
    output.mkdir(parents=True, exist_ok=False)

    def write(name, value):
        with (output / name).open("xb") as handle:
            handle.write((json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"))

    def check_unchanged():
        check_inputs()
        verify_manifest(manifest, expected_counts=counts)
        if application_identity(root) != application or capture_parser_identity(root) != parser_identity:
            raise EvaluationInputError("Frozen application or runtime changed during execution")

    write("input-freeze.json", identity)
    phase = "prediction"
    try:
        predictions, originals, execution = predict_sources(manifest, dictionary, expected_counts=counts,
            progress=lambda done, total, status: print(f"{done}/{total} {status}", flush=True))
        write("predictions.json", predictions)
        phase = "post-prediction-identity"
        check_unchanged()
        phase = "scoring"
        # Only now load expected answers; prediction is already immutable on disk.
        gold = json.loads(args.gold.read_text(encoding="utf-8"))
        predicates = json.loads(args.negative_predicates.read_text(encoding="utf-8"))
        verify_coverage(manifest, gold["coverage"])
        verify_scoring_contract(protocol, contract, predicates, expected_counts=counts, input_hashes=input_hashes, gold=gold)
        result = evaluate(gold, predictions, originals, predicates)
        write("private-matching-trace.json", result)
        phase = "post-scoring-identity"
        check_unchanged()
        write("public-report.json", {"runner_version": VERSION, "scorer_version": result["scorer_version"],
            "summary": result["summary"], "execution": {k: v for k, v in execution.items() if k != "files"},
            "input_identity": identity["inputs"], "application_head": application["head"], "dictionary": identity["dictionary"],
            "evidence_identity": {name: file_digest(output / name) for name in ("input-freeze.json", "predictions.json", "private-matching-trace.json")},
            "scope": "Fixed development source fields only; not complete-report or clinical accuracy, nor independent holdout."})
        print(json.dumps({"sources": counts[0], "pages": counts[1], "execution": result["summary"]["execution"]}), flush=True)
    except Exception as error:
        write("execution-failure.json", {"phase": phase, "error_type": type(error).__name__})
        raise


if __name__ == "__main__":
    main()
