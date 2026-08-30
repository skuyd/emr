import argparse
import csv
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile

from .normalize import is_rejected_candidate_name
from .spec import INDICATOR_SPECS


HGNC_DATASET_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"


class DictionaryPublishError(RuntimeError):
    pass


def _read_json(path):
    try:
        encoded = Path(path).read_bytes()
        value = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DictionaryPublishError("candidate_report_unavailable") from None
    if not isinstance(value, dict) or value.get("schema_version") != "1.0" or not isinstance(
        value.get("candidates"), list
    ):
        raise DictionaryPublishError("candidate_report_invalid")
    return encoded, value


def _approved_hgnc_symbols(path):
    try:
        encoded = Path(path).read_bytes()
        text = encoded.decode("utf-8")
        rows = csv.DictReader(io.StringIO(text), delimiter="\t")
        if rows.fieldnames is None or not {"symbol", "status"} <= set(rows.fieldnames):
            raise DictionaryPublishError("hgnc_dataset_invalid")
        symbols = {row["symbol"] for row in rows if row.get("status") == "Approved" and row.get("symbol")}
    except DictionaryPublishError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise DictionaryPublishError("hgnc_dataset_unavailable") from None
    if not symbols:
        raise DictionaryPublishError("hgnc_dataset_invalid")
    return encoded, frozenset(symbols)


def _validated_checked_on(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DictionaryPublishError("invalid_hgnc_checked_on") from None
    return parsed.isoformat()


def build_dictionary_payload(candidate_report, hgnc_dataset, *, hgnc_checked_on, specs=INDICATOR_SPECS):
    report_bytes, report = _read_json(candidate_report)
    hgnc_bytes, approved_symbols = _approved_hgnc_symbols(hgnc_dataset)
    checked_on = _validated_checked_on(hgnc_checked_on)
    specs = tuple(specs)
    if len(specs) != len({spec.code for spec in specs}) or len(specs) != len(
        {spec.standard_name for spec in specs}
    ):
        raise DictionaryPublishError("duplicate_indicator_spec")
    gene_symbols = {spec.standard_name for spec in specs if spec.category == "MOLECULAR_GENE"}
    if not gene_symbols <= approved_symbols:
        raise DictionaryPublishError("unapproved_gene_symbol")

    candidates = []
    for candidate in report["candidates"]:
        if not isinstance(candidate, dict):
            raise DictionaryPublishError("candidate_report_invalid")
        name = candidate.get("normalized_name")
        raw_name = candidate.get("raw_name")
        context_hash = candidate.get("context_hash")
        if (
            not isinstance(name, str)
            or not isinstance(raw_name, str)
            or not isinstance(context_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", context_hash) is None
            or is_rejected_candidate_name(name)
            or is_rejected_candidate_name(raw_name)
        ):
            raise DictionaryPublishError("unsafe_candidate_report")
        candidates.append((name, context_hash))

    indicators = []
    for spec in specs:
        try:
            selector = re.compile(spec.selector_pattern, re.IGNORECASE)
        except re.error:
            raise DictionaryPublishError("invalid_indicator_selector") from None
        evidence = sorted({context_hash for name, context_hash in candidates if selector.search(name)})[:3]
        if not evidence:
            raise DictionaryPublishError(f"missing_indicator_evidence:{spec.code}")
        indicators.append(
            {
                "aliases": list(spec.aliases),
                "capability_level": spec.capability_level,
                "category": spec.category,
                "code": spec.code,
                "evidence_context_hashes": evidence,
                "ocr_variants": list(spec.ocr_variants),
                "standard_name": spec.standard_name,
                "unit_forms": list(spec.unit_forms),
            }
        )

    return {
        "dictionary_version": "1.0.0",
        "generated_from": {
            "candidate_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "hgnc_checked_on": checked_on,
            "hgnc_dataset_sha256": hashlib.sha256(hgnc_bytes).hexdigest(),
            "hgnc_dataset_url": HGNC_DATASET_URL,
        },
        "indicators": sorted(indicators, key=lambda item: item["code"]),
        "schema_version": "1.0",
    }


def render_dictionary_json(payload):
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_dictionary(path, payload):
    path = Path(path)
    encoded = render_dictionary_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".lab-dictionary-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return hashlib.sha256(encoded).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish the reviewed, versioned laboratory indicator dictionary")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--hgnc", required=True)
    parser.add_argument("--hgnc-checked-on", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    payload = build_dictionary_payload(
        arguments.candidates,
        arguments.hgnc,
        hgnc_checked_on=arguments.hgnc_checked_on,
    )
    content_hash = write_dictionary(arguments.output, payload)
    conventional = sum(item["category"] != "MOLECULAR_GENE" for item in payload["indicators"])
    genes = len(payload["indicators"]) - conventional
    print(f"indicators={len(payload['indicators'])} conventional={conventional} genes={genes} sha256={content_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
