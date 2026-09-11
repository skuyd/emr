from apps.exports.content import SCHEMA_VERSION
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

from pypdf import PdfReader
import pytest

from apps.exports.content import build_snapshot
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def _pdf(payload, artifact_name):
    import pypdfium2

    # Read actual rendered text boxes as well as the PDF text stream.
    with pypdfium2.PdfDocument(payload) as document:
        for page in document:
            width, height = page.get_size()
            text = page.get_textpage()
            for index in range(text.count_chars()):
                left, bottom, right, top = text.get_charbox(index)
                if left == right or top == bottom:
                    continue
                assert -1 <= left <= right <= width + 1
                assert -1 <= bottom <= top <= height + 1
            text.close()
            page.close()
    if os.environ.get("PHR_PHASE_THREE_ARTIFACT_DIR"):
        directory = Path(os.environ["PHR_PHASE_THREE_ARTIFACT_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / artifact_name).write_bytes(payload)
    return PdfReader(io.BytesIO(payload))


def _snapshot(django_user_model, marker, text="诊断：考虑炎症；未见明确转移。", *, details=False):
    _, patient = _patient(django_user_model, marker)
    document, version = parsed_facts(patient, [text, "分期：待定。"])
    fact = Fact.objects.get(parsing_version=version, category="DIAGNOSIS")
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all", "details": details, "basic_info": "阈值 ≤4.20；≥2；<3；>1"})
    return patient, document, snapshot


def test_pdf_is_a4_readable_chinese_with_qualifiers_and_source(django_user_model):
    from apps.exports.pdf import render_pdf

    _, document, snapshot = _snapshot(django_user_model, "pdf-chinese")
    payload = render_pdf(snapshot)
    reader = _pdf(payload, "card-chinese-symbols.pdf")
    assert len(reader.pages) == 1
    assert abs(float(reader.pages[0].mediabox.width) - 595.276) < 1
    assert abs(float(reader.pages[0].mediabox.height) - 841.890) < 1
    text = reader.pages[0].extract_text()
    for value in ("就诊速查卡", "考虑炎症", "未见明确转移", "≤4.20", "≥2", "<3", ">1",
                  "诊断与分期", "治疗时间线", "重点检验", "影像、病理与分子检测", "来源信息",
                  "当前所选资料中暂无可用信息", document.display_filename, "第 1 页"):
        assert value in text
    assert "分期：待定" not in text
    assert snapshot["generated_at"][:10] in text


def test_overflow_requires_explicit_appendix_and_never_silently_loses_text(django_user_model):
    from apps.exports.errors import PdfUnavailable
    from apps.exports.pdf import render_pdf

    text = "诊断：" + "未见明确异常，尚不能排除炎症。" * 150 + "完整末尾标记。"
    _, _, snapshot = _snapshot(django_user_model, "pdf-overflow", text)
    with pytest.raises(PdfUnavailable, match="附页"):
        render_pdf(snapshot)
    snapshot["card"]["details"] = True
    reader = _pdf(render_pdf(snapshot), "card-overflow-appendix.pdf")
    combined = "".join(page.extract_text() for page in reader.pages).replace("\n", "")
    assert len(reader.pages) > 1
    assert "完整末尾标记" in combined
    assert combined.count("尚不能排除炎症") == 150
    assert "附页" in reader.pages[0].extract_text()
    assert "来源信息" in reader.pages[0].extract_text()


def test_json_csv_preserve_strings_relations_nulls_and_guard_formulas(django_user_model):
    from apps.exports.formats import csv_tables, json_bytes

    _, _, snapshot = _snapshot(django_user_model, "structured")
    snapshot["facts"][0]["content"]["text"] = '=危险公式("合成"),\n保留第二行'
    data = json.loads(json_bytes(snapshot))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["facts"][0]["content"]["text"].startswith("=")
    assert all(row["raw_text"] != "分期：待定。" for row in data["sources"])
    assert data["facts"][0]["content"]["date"] is None
    assert "dependency_fingerprint" not in data and "source_token" not in json_bytes(snapshot).decode()
    assert "url" not in data["facts"][0]["source"]
    tables = csv_tables(snapshot)
    rows = list(csv.DictReader(io.StringIO(tables["facts.csv"].decode("utf-8-sig"))))
    assert rows[0]["text"] == '\'=危险公式("合成"),\n保留第二行'
    assert rows[0]["document_id"] == snapshot["documents"][0]["id"]
    assert list(csv.DictReader(io.StringIO(tables["documents.csv"].decode("utf-8-sig"))))[0]["id"] == rows[0]["document_id"]


def test_zip_originals_are_exact_duplicate_filenames_unique_with_verified_manifest(django_user_model):
    from apps.exports.formats import build_artifact

    _, patient = _patient(django_user_model, "zip-originals")
    store = InMemoryObjectStore()
    documents = [_document(patient)[0], _document(patient)[0]]
    for index, document in enumerate(documents):
        payload = b"synthetic-original-" + bytes([index])
        store.objects[document.original_object_key] = payload
        document.byte_size, document.sha256 = len(payload), hashlib.sha256(payload).hexdigest()
        # Synthetic objects were created without upload; seed their immutable identity once.
        type(document).objects.filter(pk=document.pk).update(byte_size=document.byte_size, sha256=document.sha256)
    snapshot = build_snapshot(patient, {"mode": "all"})
    artifact = build_artifact(snapshot, {"format": "zip", "parts": ["originals", "json", "csv"]}, store)
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert len(bundle.namelist()) == len(set(bundle.namelist()))
        original_entries = [item for item in manifest["files"] if item["kind"] == "original"]
        assert len(original_entries) == 2
        for entry in manifest["files"]:
            payload = bundle.read(entry["path"])
            assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
            assert entry["byte_size"] == len(payload)
        for document in documents:
            entry = next(item for item in original_entries if item["document_id"] == str(document.pk))
            assert entry["original_filename"] == document.display_filename
            assert bundle.read(entry["path"]) == store.objects[document.original_object_key]
    single = build_artifact(build_snapshot(patient, {"mode": "documents", "document_ids": [str(documents[0].pk)]}),
                            {"format": "original"}, store)
    assert single.payload == store.objects[documents[0].original_object_key]
    assert single.filename == documents[0].display_filename


def test_missing_original_fails_whole_bundle_and_no_empty_zip(django_user_model):
    from apps.exports.formats import build_artifact
    from apps.exports.errors import ExportInputError
    from apps.documents.errors import ObjectNotFound, IntegrityMismatch

    _, document, snapshot = _snapshot(django_user_model, "missing-original")
    store = InMemoryObjectStore()
    with pytest.raises(ObjectNotFound):
        build_artifact(snapshot, {"format": "zip", "parts": ["json", "originals"]}, store)
    with pytest.raises(ExportInputError):
        build_artifact(snapshot, {"format": "zip", "parts": []}, store)
    store.objects[document.original_object_key] = b"wrong content"
    with pytest.raises(IntegrityMismatch):
        build_artifact(snapshot, {"format": "original"}, store)


def test_pdf_lab_table_keeps_comparator_percent_zero_and_quality_labels(django_user_model):
    from datetime import date
    from apps.exports.pdf import render_pdf
    from tests.labs.test_trends import _observation

    _, patient = _patient(django_user_model, "pdf-labs")
    for value, kind in (("≤4.20", "COMPARATOR"), ("阴性", "QUALITATIVE"), ("0", "NUMERIC"), ("2+", "SEMI_QUANTITATIVE")):
        _observation(patient, date(2026, 8, 20), value, result_type=kind, raw_unit="%")
    snapshot = build_snapshot(patient, {"mode": "all", "details": True})
    reader = _pdf(render_pdf(snapshot), "card-special-labs.pdf")
    text = "\n".join(page.extract_text() for page in reader.pages)
    for value in ("≤4.20", "阴性", "0", "2+", "%", "参考范围", "依据不足"):
        assert value in text
    assert len(snapshot["card"]["lab_ids"]) == 4


@pytest.mark.parametrize("prefix", ["", " ", "\t", "\r", "\n", "\ufeff", "\u00a0", "\x01"])
def test_csv_guards_formula_after_whitespace_and_controls(prefix):
    from apps.exports.formats import _csv_cell

    assert _csv_cell(prefix + "=1+1").startswith("'")


def test_card_keeps_multiple_treatment_dates_and_traceability_when_source_section_is_omitted(django_user_model):
    from apps.exports.pdf import render_pdf

    _, patient = _patient(django_user_model, "pdf-dates")
    document, version = parsed_facts(patient, ["治疗方案：2025年8月行方案甲，2026年1月行方案乙。"])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all", "sections": ["patient", "treatment"]})
    reader = PdfReader(io.BytesIO(render_pdf(snapshot)))
    text = "\n".join(page.extract_text() for page in reader.pages).replace("\n", "")
    assert "2025年8月" in text and "2026年1月" in text
    assert "未记载明确时间" not in text
    assert document.display_filename in text and str(document.pk) in text
