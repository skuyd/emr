"""Synthetic clinical scope regressions; no original reports or gold fixtures."""

from copy import deepcopy

import pytest

from apps.facts.clinical_extraction import field_candidates
from apps.facts.clinical_segments import segment_reports
from tests.facts.test_clinical_segments import block


def extract(body):
    reports, unparsed = segment_reports([block(
        "CT诊断报告书\n影像表现：" + body + "\n诊断意见：请核对原件。")])
    assert len(reports) == 1 and not unparsed
    return field_candidates(reports[0])


def sites(fields):
    return [field.value["text"] for field in fields if field.key == "lesion.site"]


@pytest.mark.parametrize("separator", ["；", ";"])
def test_semicolon_separates_normal_organ_from_unmeasured_focal_location(separator):
    fields = extract(f"左肾未见异常{separator}右肺见结节。")
    assert sites(fields) == ["右肺"]
    source = next(field for field in fields if field.key == "lesion.site")
    assert "左肾" not in source.raw_value
    assert source.raw_value == "右肺见结节。"


@pytest.mark.parametrize("body", [
    "肝外评估：淋巴结：右肾旁见淋巴结。",
    "左肺未见异常；肝外评估：淋巴结：右肾旁见淋巴结。",
    "肝外评估：软组织：右肾旁见结节。",
])
def test_chapter_and_focal_category_labels_are_not_local_finding_anatomy(body):
    assert sites(extract(body)) == ["右肾"]


@pytest.mark.parametrize(("group", "expected"), [
    ("左肺及右肺", "左肺及右肺"),
    ("左肾与右肾", "左肾与右肾"),
    ("肝及左肾与右肾", "肝及左肾与右肾"),
    ("纵隔（4R、7组）及双肺门", "纵隔(4R、7组)及双肺门"),
])
def test_unmeasured_explicit_site_group_retains_every_named_member(group, expected):
    assert sites(extract(group + "见结节。")) == [expected]


@pytest.mark.parametrize(("body", "expected"), [
    ("结节位于右肺。", "右肺"),
    ("结节位于左肺及右肺。", "左肺及右肺"),
])
def test_explicit_postposed_finding_location_does_not_require_an_invented_prefix(body, expected):
    assert sites(extract(body)) == [expected]


@pytest.mark.parametrize(("body", "expected"), [
    ("肝内回声不均，S6段见低回声区，约12mm。", "肝S6"),
    ("肝回声不均，右叶见结节，约12mm。", "肝右叶"),
])
def test_relative_segment_or_leaf_keeps_explicit_organ_context_across_a_comma(body, expected):
    fields = extract(body)
    assert sites(fields) == [expected]
    site = next(field for field in fields if field.key == "lesion.site")
    assert site.transformations[0]["rule"] == "same_clause_explicit_organ_and_segment"
    assert body[:-1] in site.raw_value


@pytest.mark.parametrize("negative", [
    "未见明显肿大", "不见明确肿大", "无明显肿大", "未见明显肿", "未见肿大或异常浓聚",
])
def test_local_stacked_or_coordinated_negative_modifiers_do_not_create_focal_candidate(negative):
    assert sites(extract("纵隔" + negative + "淋巴结。")) == []


@pytest.mark.parametrize(("body", "expected"), [
    ("纵隔未见明显肿大淋巴结，但右肺见结节。", ["右肺"]),
    ("右肺见结节，纵隔未见明显肿大淋巴结。", ["右肺"]),
    ("左肺未见结节，但见肿块。", ["左肺"]),
    ("左肺见结节，未见明显肿大淋巴结。", ["左肺"]),
    ("纵隔未见结节及肿块，右肺见结节。", ["右肺"]),
    ("左肺见结节，右肺见肿块。", ["左肺", "右肺"]),
])
def test_local_negative_scope_keeps_other_affirmative_findings(body, expected):
    assert sites(extract(body)) == expected


def test_negative_target_measurement_is_not_borrowed_by_an_earlier_positive_finding():
    fields = extract("左肺见结节，约6mm，但右肺未见明显肿大淋巴结，短径12mm。")
    assert sites(fields) == ["左肺"]
    assert [field.value["components"][0]["value"] for field in fields
            if field.key == "lesion.dimensions"] == ["6"]


@pytest.mark.parametrize("separator", ["；", ";"])
def test_scoped_dimensions_suv_history_and_explicit_maximum_keep_their_own_entity(separator):
    fields = extract("左肺见结节，原大小12×9mm，现大小8×6mm，"
                     "既往检查SUVmax5.4，本次SUVmax3.2，为本报告最大病灶"
                     + separator + "右肺见结节，约6mm，SUVmax2.1。")
    locations = {field.value["text"]: field.entity for field in fields if field.key == "lesion.site"}
    assert set(locations) == {"左肺", "右肺"}
    dimensions = [field for field in fields if field.key == "lesion.dimensions"]
    assert [(field.entity, field.value["measurement_role"],
             [component["value"] for component in field.value["components"]]) for field in dimensions] == [
        (locations["左肺"], "HISTORICAL", ["12", "9"]),
        (locations["左肺"], "CURRENT", ["8", "6"]),
        (locations["右肺"], "CURRENT", ["6"]),
    ]
    assert [(field.entity, field.value["measurement_role"], field.value["values"])
            for field in fields if field.key == "lesion.suvmax"] == [
        (locations["左肺"], "HISTORICAL", ["5.4"]),
        (locations["左肺"], "CURRENT", ["3.2"]),
        (locations["右肺"], "CURRENT", ["2.1"]),
    ]
    assert [(field.entity, field.value["code"]) for field in fields if field.key == "lesion.maximum_scope"] == [
        (locations["左肺"], "REPORT_MAXIMUM"),
    ]
    assert all("右肺" not in field.raw_value for field in fields
               if field.entity == locations["左肺"] and field.key.startswith("lesion."))


def test_suv_extension_does_not_recreate_a_negated_morphological_target():
    fields = extract("纵隔未见明显肿大淋巴结，SUVmax1.0；右肺见结节，约6mm，SUVmax3.0。")
    assert sites(fields) == ["右肺"]
    assert [field.value["values"] for field in fields if field.key == "lesion.suvmax"] == [["3.0"]]


@pytest.mark.django_db
def test_scoped_multiblock_fields_keep_original_unicode_offsets_and_source_geometry(django_user_model):
    from apps.facts.clinical_extraction import extract_clinical_version
    from apps.facts.models import Fact
    from tests.documents.test_detail_viewer import _patient
    from tests.facts.factories import parsed_facts

    _, patient = _patient(django_user_model, "clinical-scope-offsets")
    texts = ["CT诊断报告书", "影像表现：左肾未见异常；右肺见Ⅲ类", "结节，约１２×８ｍｍ。", "诊断意见：请核对原件。"]
    document, version = parsed_facts(patient, texts, document_type="IMAGING")
    blocks = list(version.ocr_blocks.order_by("reading_order"))
    originals = {source.pk: (source.text, deepcopy(source.polygon)) for source in blocks}
    extract_clinical_version(version)
    site = Fact.objects.get(document=document, field_key="lesion.site")
    assert site.automatic_content["value"]["text"] == "右肺"
    assert "左肾" not in site.raw_text and "Ⅲ" in site.raw_text
    fragments = list(site.source_fragments.order_by("ordinal"))
    assert len(fragments) == 2
    assert fragments[0].start_offset == texts[1].index("右肺")
    assert fragments[0].end_offset == len(texts[1])
    assert fragments[1].start_offset == 0
    dimension = Fact.objects.get(document=document, field_key="lesion.dimensions")
    assert dimension.automatic_content["value"]["raw"] == "１２×８ｍｍ"
    assert [component["value"] for component in dimension.automatic_content["value"]["components"]] == ["12", "8"]
    for field in (site, dimension):
        assert field.revision_number == 0
        for fragment in field.source_fragments.all():
            original, polygon = originals[fragment.ocr_block_id]
            assert fragment.raw_text == original[fragment.start_offset:fragment.end_offset]
            assert fragment.polygon == polygon
            assert fragment.evidence.source_text == fragment.raw_text
            fragment.full_clean()
    for source in blocks:
        source.refresh_from_db()
        assert (source.text, source.polygon) == originals[source.pk]


@pytest.mark.django_db
def test_new_extractor_rules_leave_completed_confirmed_fields_and_relationships_untouched(django_user_model, monkeypatch):
    from apps.facts import clinical_extraction
    from apps.facts.models import Fact, FactRevision
    from apps.lesions.models import LesionObservationRevision
    from apps.lesions.readmodels import review_observations
    from apps.lesions.services import create_lesion
    from tests.lesions.factories import imaging_observation

    # A completed persisted run retains its historical rule identity. The simple
    # synthetic body is valid under both generations; no old parser is replayed.
    with monkeypatch.context() as old_rule:
        old_rule.setattr(clinical_extraction, "EXTRACTOR_VERSION", "clinical-imaging-v2")
        patient, document, report = imaging_observation(django_user_model, name="completed-scope-rule")
    row = review_observations(patient, actor=patient.account)[0]
    create_lesion(patient, actor=patient.account, observation_id=row["id"], expected_revision=0,
                  expected_source=row["source_token"], name="合成观察 A", checked_original=True)
    before = deepcopy(review_observations(patient, actor=patient.account))
    fields_before = list(Fact.objects.filter(clinical_report=report).values())
    revisions_before = list(FactRevision.objects.values())
    assignments_before = list(LesionObservationRevision.objects.values())
    completed = report.parsing_version.clinical_extraction

    def must_not_extract_again(_segment):
        raise AssertionError("An already completed extraction must not rebuild confirmed fields.")

    with monkeypatch.context() as frozen:
        frozen.setattr(clinical_extraction, "field_candidates", must_not_extract_again)
        same = clinical_extraction.extract_clinical_version(report.parsing_version)
    assert same.pk == completed.pk and same.extractor_version == "clinical-imaging-v2"
    assert list(Fact.objects.filter(clinical_report=report).values()) == fields_before
    assert list(FactRevision.objects.values()) == revisions_before
    assert list(LesionObservationRevision.objects.values()) == assignments_before
    assert review_observations(patient, actor=patient.account) == before
    _, fresh_document, fresh_report = imaging_observation(django_user_model, patient=patient, day="2026-09-01")
    assert fresh_report.parsing_version.clinical_extraction.extractor_version != completed.extractor_version
    assert fresh_document.pk != document.pk
