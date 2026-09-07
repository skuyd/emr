"""Unselected report bodies cannot hitchhike through a selected document."""

from copy import deepcopy
from uuid import uuid4

import pytest

from apps.exports.errors import ExportInputError
from apps.patients.sharing_content import normalize_scope, project_snapshot


def material():
    document, report, other_report, field, other_field = [str(uuid4()) for _ in range(5)]
    snapshot = {
        "schema_version": "1.1", "patient_id": str(uuid4()), "generated_at": "synthetic", "dependency_fingerprint": "opaque",
        "documents": [{"id": document, "filename": "synthetic.png"}], "patient": {"nickname": "Synthetic"},
        "facts": [], "labs": [], "card": {"lab_ids": []},
        "excluded_documents": [{"filename": "private unselected file"}],
        "clinical_reports": [{"id": report, "document_id": document, "title": "selected header", "body": "private report body"},
                             {"id": other_report, "document_id": document, "title": "private other report"}],
        "clinical_fields": [{"id": field, "report_id": report, "category": "IMAGING", "content": {"text": "selected field"}},
                            {"id": other_field, "report_id": other_report, "category": "DIAGNOSIS", "content": {"text": "private diagnosis"}}],
        "clinical_field_sources": [{"fact_id": field, "report_id": report, "document_id": document, "raw_text": "private neighboring field context"},
                                   {"fact_id": other_field, "report_id": other_report, "document_id": document, "raw_text": "private other evidence"}],
    }
    return snapshot, document, report, field


def test_explicit_field_share_filters_other_report_sections_and_source_arrays():
    snapshot, document, report, field = material()
    scope = normalize_scope({"document_ids": [document], "sections": ["imaging"], "clinical_field_ids": [field]})
    public = project_snapshot(snapshot, scope)
    assert [row["id"] for row in public["clinical_fields"]] == [field]
    assert [row["id"] for row in public["clinical_reports"]] == [report]
    assert len(public["clinical_field_sources"]) == 1
    assert "raw_text" not in public["clinical_field_sources"][0]
    assert "private" not in repr(public)
    assert public["patient"] == {}


def test_unchecked_display_section_does_not_release_selected_report_body_or_header():
    snapshot, document, report, _ = material()
    scope = normalize_scope({"document_ids": [document], "sections": ["labs"], "report_ids": [report]})
    public = project_snapshot(snapshot, scope)
    assert public["clinical_fields"] == public["clinical_reports"] == public["clinical_field_sources"] == []


@pytest.mark.parametrize("key", ["fact_ids", "lab_ids", "report_ids", "clinical_field_ids"])
def test_partial_selection_cannot_open_full_source_or_silently_expand_empty_scope(key):
    document, identity = str(uuid4()), str(uuid4())
    with pytest.raises(ExportInputError):
        normalize_scope({"document_ids": [document], "sections": ["sources"], key: [identity]})
    with pytest.raises(ExportInputError):
        normalize_scope({"document_ids": [document], "sections": ["imaging"], key: []})


def test_unknown_report_field_is_rejected_instead_of_expanding_to_full_document():
    snapshot, document, _, _ = material()
    for key in ("report_ids", "clinical_field_ids"):
        with pytest.raises(ExportInputError):
            project_snapshot(deepcopy(snapshot), normalize_scope({"document_ids": [document], "sections": ["imaging"], key: [str(uuid4())]}))
