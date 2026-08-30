import json

import pytest

from tools.sample_dictionary.publish import (
    DictionaryPublishError,
    build_dictionary_payload,
    render_dictionary_json,
)
from tools.sample_dictionary.spec import IndicatorSpec


def _candidate_report(path, candidates):
    path.write_text(
        json.dumps({"schema_version": "1.0", "candidates": candidates}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _hgnc(path, *symbols):
    rows = ["symbol\tstatus\tname", *(f"{symbol}\tApproved\tSynthetic approved gene" for symbol in symbols)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _candidate(name, context, source="f" * 64):
    return {
        "context_hash": context,
        "normalized_name": name,
        "raw_name": name,
        "source_file_hash": source,
    }


def test_publisher_is_deterministic_and_keeps_only_context_hash_evidence(tmp_path):
    report = tmp_path / "candidates.json"
    hgnc = tmp_path / "hgnc.tsv"
    _candidate_report(
        report,
        [
            _candidate("WBC 白细胞", "a" * 64),
            _candidate("BRCA1", "b" * 64),
        ],
    )
    _hgnc(hgnc, "BRCA1")
    specs = (
        IndicatorSpec("LAB_WBC", "白细胞计数", r"WBC", ("WBC",), unit_forms=("10^9/L",)),
        IndicatorSpec(
            "GENE_BRCA1",
            "BRCA1",
            r"(^|[^A-Z0-9])BRCA1([^A-Z0-9]|$)",
            ("BRCA1",),
            category="MOLECULAR_GENE",
            capability_level="SEARCH_ONLY",
        ),
    )

    first = build_dictionary_payload(report, hgnc, hgnc_checked_on="2026-08-31", specs=specs)
    second = build_dictionary_payload(report, hgnc, hgnc_checked_on="2026-08-31", specs=specs)
    encoded = render_dictionary_json(first)

    assert encoded == render_dictionary_json(second)
    assert [item["code"] for item in first["indicators"]] == ["GENE_BRCA1", "LAB_WBC"]
    assert first["indicators"][0]["evidence_context_hashes"] == ["b" * 64]
    assert b"source_file_hash" not in encoded
    assert b"raw_name" not in encoded


def test_publisher_rejects_missing_evidence_unapproved_genes_and_unsafe_raw_names(tmp_path):
    report = tmp_path / "candidates.json"
    hgnc = tmp_path / "hgnc.tsv"
    _hgnc(hgnc, "BRCA1")
    missing = IndicatorSpec("LAB_MISSING", "合成缺失指标", r"NOT-PRESENT", ("NOT-PRESENT",))
    _candidate_report(report, [_candidate("WBC", "a" * 64)])
    with pytest.raises(DictionaryPublishError, match="missing_indicator_evidence"):
        build_dictionary_payload(report, hgnc, hgnc_checked_on="2026-08-31", specs=(missing,))

    unapproved = IndicatorSpec(
        "GENE_NOTREAL",
        "NOTREAL",
        r"NOTREAL",
        ("NOTREAL",),
        category="MOLECULAR_GENE",
        capability_level="SEARCH_ONLY",
    )
    with pytest.raises(DictionaryPublishError, match="unapproved_gene_symbol"):
        build_dictionary_payload(report, hgnc, hgnc_checked_on="2026-08-31", specs=(unapproved,))

    _candidate_report(report, [_candidate("白细胞 123456", "a" * 64)])
    with pytest.raises(DictionaryPublishError, match="unsafe_candidate_report"):
        build_dictionary_payload(report, hgnc, hgnc_checked_on="2026-08-31", specs=(missing,))
