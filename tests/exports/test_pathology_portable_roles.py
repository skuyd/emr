"""Portable role copies must agree without retrofitting old field schemas."""
import json

import pytest

from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import json_bytes, read_structured_data
from apps.exports.treatment import ARRAYS as TREATMENT_ARRAYS
from apps.glucose.output import ARRAYS as GLUCOSE_ARRAYS
from tests.exports.test_pathology_exports import _graph, selection


@pytest.fixture
def portable(django_user_model, db):
    _, patient, document, _, fields = _graph(django_user_model, "portable-source-role")
    snapshot = build_snapshot(patient, {**selection(document, fields["cps"]),
        "clinical_field_ids": [str(fields[key].pk) for key in ("cps", "clone")]})
    return json.loads(json_bytes(snapshot))


def _content(data, field_key):
    return next(row["content"] for row in data["clinical_fields"] if row["field_key"] == field_key)


@pytest.mark.parametrize("field_key", ["ihc.score", "assay.antibody"])
@pytest.mark.parametrize("change", ["content_control", "content_missing", "bundle_missing", "allowed_but_conflicting"])
def test_reader_rejects_missing_or_contradictory_source_role_copies(portable, field_key, change):
    content = _content(portable, field_key)
    assert content["source_role"] == content["semantic_qualifiers"]["source_role"] == "CURRENT_RESULT"
    if change == "content_control":
        content["source_role"] = "CONTROL"
    elif change == "content_missing":
        content.pop("source_role")
    elif change == "bundle_missing":
        content["semantic_qualifiers"].pop("source_role")
    else:
        content["source_role"] = "PRIMARY_ASSAY_METADATA"
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(portable))


@pytest.mark.parametrize("field_key,role", [
    ("ihc.score", "PRIMARY_ASSAY_METADATA"),
    ("assay.antibody", "CONTROL"), ("assay.antibody", "HISTORICAL_QUOTE"),
    ("assay.antibody", "UNKNOWN"), ("assay.antibody", None),
])
def test_reader_rejects_matching_roles_not_allowed_for_selected_field(portable, field_key, role):
    content = _content(portable, field_key)
    content["source_role"] = content["semantic_qualifiers"]["source_role"] = role
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(portable))


@pytest.mark.parametrize("metadata_role", ["CURRENT_RESULT", "PRIMARY_ASSAY_METADATA"])
def test_reader_keeps_current_score_and_each_allowed_metadata_role(portable, metadata_role):
    content = _content(portable, "assay.antibody")
    content["source_role"] = content["semantic_qualifiers"]["source_role"] = metadata_role
    original = json.dumps(portable)
    assert read_structured_data(original) == portable
    assert json.dumps(portable) == original


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2", "1.3", "1.4"])
def test_legacy_fields_need_no_new_pathology_role_or_context(version):
    # These are valid old-schema data shapes, not downgraded pathology rows.
    old_field = {"field_key": "lesion.site", "schema_version": "1.0",
                 "content": {"field_key": "lesion.site", "value": {"text": "合成原文位置"}}}
    data = {"schema_version": version, "documents": [], "facts": [], "labs": [], "sources": []}
    if version != "1.0":
        data.update(clinical_reports=[], clinical_fields=[old_field], clinical_field_sources=[])
    if version not in {"1.0", "1.1"}:
        data["self_records"] = []
    if version in {"1.3", "1.4"}:
        data.update({key: [] for key in TREATMENT_ARRAYS})
    if version == "1.4":
        data.update({key: [] for key in GLUCOSE_ARRAYS})
    original = json.dumps(data)
    result = read_structured_data(original)
    assert result["clinical_fields"] == ([] if version == "1.0" else [old_field])
    assert json.dumps(data) == original
