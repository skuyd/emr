from datetime import date
import uuid

from django.utils import timezone
import pytest

from apps.documents.models import DocumentStatus, ProcessingRun, ProcessingStage
from apps.labs import trends
from apps.labs.models import CapabilityLevel, LabObservation, ResultType
from apps.labs.trends import eligible_trend_codes, trend_view
from apps.labs.quality import QUALITY_POLICY_VERSION
from apps.processing.models import (
    DatePrecision,
    DocumentMetadataCandidate,
    DocumentSummary,
    DocumentType,
    MetadataKind,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def _observation(
    patient,
    observation_date,
    raw_value,
    *,
    code="LAB_WBC",
    standard_name="白细胞计数",
    raw_name="白细胞",
    raw_unit="10^9/L",
    method="合成方法A",
    institution="合成检验中心",
    result_type=ResultType.NUMERIC,
    precision=DatePrecision.DAY,
    capability=CapabilityLevel.STABLE,
):
    document, pages = _document(patient, page_count=1, status=DocumentStatus.ORGANIZED)
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
        stage=ProcessingStage.SUCCEEDED,
        finished_at=timezone.now(),
        is_current=True,
    )
    version = ParsingVersion.objects.create(
        document=document,
        processing_run=run,
        parser_version="parser-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        dictionary_version="1.0.0",
        dictionary_hash="a" * 64,
        status=ParsingVersionStatus.READY,
        diagnostics={"quality_policy": QUALITY_POLICY_VERSION},
    )
    ParsingVersion.objects.activate(version)
    DocumentSummary.objects.create(
        parsing_version=version,
        document_type=DocumentType.LAB,
        document_date_raw=observation_date.isoformat(),
        document_date=observation_date,
        date_precision=precision,
        institution_raw=institution,
        confidence="0.9000",
    )
    evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=pages[0],
        polygon=((0.1, 0.2), (0.6, 0.2), (0.6, 0.3), (0.1, 0.3)),
        source_text="synthetic evidence",
        confidence="0.9800",
    )
    date_evidence = SourceEvidence.objects.create(
        parsing_version=version,
        document_page=pages[0],
        source_text="采样日期：" + observation_date.isoformat(),
        confidence="0.9800",
    )
    DocumentMetadataCandidate.objects.create(
        parsing_version=version,
        kind=MetadataKind.DOCUMENT_DATE,
        raw_text=date_evidence.source_text,
        normalized_value=observation_date.isoformat(),
        precision=precision,
        confidence="0.9800",
        evidence=date_evidence,
        selected=True,
    )
    observation = LabObservation.objects.create(
        parsing_version=version,
        document_page=pages[0],
        evidence=evidence,
        reading_order=1,
        raw_name=raw_name,
        standard_code=code,
        standard_name=standard_name,
        raw_value=raw_value,
        result_type=result_type,
        raw_unit=raw_unit,
        observation_date=observation_date,
        institution_raw=institution,
        method_raw=method,
        capability_level=capability,
        dictionary_version="1.0.0",
    )
    return document, observation


def test_trend_summaries_include_only_current_patients_eligible_codes(django_user_model):
    _client, patient = _patient(django_user_model, "summary-owner")
    _other_client, other = _patient(django_user_model, "summary-other")
    _observation(patient, date(2026, 7, 1), "4.200")
    _document, latest = _observation(patient, date(2026, 8, 20), "5.0", raw_name="WBC")
    _observation(patient, date(2026, 8, 21), "88", code="LAB_SINGLE", standard_name="单次指标")
    _observation(other, date(2026, 7, 1), "99.1", code="LAB_OTHER")
    _observation(other, date(2026, 8, 1), "99.2", code="LAB_OTHER")

    summaries = trends.trend_summaries(patient)

    assert [(item.standard_code, item.standard_name) for item in summaries] == [("LAB_WBC", "白细胞计数")]
    assert summaries[0].latest_observation == latest
    assert summaries[0].point_count == 2


def test_trend_summaries_use_one_candidate_query(django_user_model, django_assert_num_queries):
    _client, patient = _patient(django_user_model, "summary-query")
    _observation(patient, date(2026, 7, 1), "4.2")
    _observation(patient, date(2026, 8, 1), "4.6")

    with django_assert_num_queries(1):
        summaries = trends.trend_summaries(patient)

    assert len(summaries) == 1


def test_trend_summaries_order_codes_by_newest_observation_then_name(django_user_model):
    _client, patient = _patient(django_user_model, "summary-order")
    _observation(patient, date(2026, 7, 1), "4.2", code="LAB_Z", standard_name="Zulu")
    _observation(patient, date(2026, 9, 1), "4.6", code="LAB_Z", standard_name="Zulu")
    _observation(patient, date(2026, 7, 1), "1.2", code="LAB_B", standard_name="Beta")
    _observation(patient, date(2026, 8, 1), "1.6", code="LAB_B", standard_name="Beta")
    _observation(patient, date(2026, 7, 1), "2.2", code="LAB_A", standard_name="Alpha")
    _observation(patient, date(2026, 8, 1), "2.6", code="LAB_A", standard_name="Alpha")

    summaries = trends.trend_summaries(patient)

    assert [(item.standard_code, item.standard_name) for item in summaries] == [
        ("LAB_Z", "Zulu"),
        ("LAB_A", "Alpha"),
        ("LAB_B", "Beta"),
    ]


def test_trend_summaries_count_points_across_included_series(django_user_model):
    _client, patient = _patient(django_user_model, "summary-series")
    _observation(patient, date(2026, 7, 1), "1.0", raw_unit="mg/L")
    _observation(patient, date(2026, 8, 1), "2.0", raw_unit="mg/L")
    _observation(patient, date(2026, 7, 2), "0.1", raw_unit="mmol/L")
    _observation(patient, date(2026, 8, 2), "0.2", raw_unit="mmol/L")

    summaries = trends.trend_summaries(patient)

    assert summaries[0].point_count == 4


def test_eligible_trend_preserves_raw_values_and_each_point_links_to_evidence(django_user_model):
    client, patient = _patient(django_user_model, "t")
    first_document, first = _observation(patient, date(2026, 7, 1), "4.200")
    _second_document, second = _observation(patient, date(2026, 8, 20), "5.0", raw_name="WBC")

    detail = client.get(f"/records/{first_document.pk}/").content.decode()
    response = client.get("/trends/LAB_WBC/")
    content = response.content.decode()

    assert 'href="/trends/LAB_WBC/"' in detail
    assert response.status_code == 200
    for expected in (
        "白细胞计数",
        "白细胞、WBC",
        "4.200",
        "5.0",
        "10^9/L",
        "2026年7月1日",
        "2026年8月20日",
        "合成检验中心",
        "这里只帮助你看看指标随时间的变化，不提供诊断、治疗或疗效结论。",
        f"evidence={first.evidence_id}",
        f"evidence={second.evidence_id}",
    ):
        assert expected in content
    assert content.index("2026年7月1日") < content.index("2026年8月20日")
    assert "改善" not in content and "恶化" not in content and "持续升高" not in content
    assert response["Cache-Control"] == "private, no-store, max-age=0"


def test_trend_copy_is_neutral_and_explains_source_first_use(django_user_model):
    client, patient = _patient(django_user_model, "t2")
    _observation(patient, date(2026, 7, 1), "4.200")
    _observation(patient, date(2026, 8, 20), "5.0", raw_name="WBC")

    response = client.get("/trends/LAB_WBC/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "看看指标随时间的变化" in content
    assert "查看来源原件" in content
    assert "不提供诊断、治疗或疗效结论" in content
    assert "实验性整理结果" not in content


def test_missing_method_is_eligible_only_with_same_institution_and_no_known_conflict(django_user_model):
    _client, patient = _patient(django_user_model, "u")
    _observation(patient, date(2026, 7, 1), "4.2", method="")
    _observation(patient, date(2026, 8, 1), "4.6", method="")

    trend = trend_view(patient, "LAB_WBC")

    assert trend is not None
    assert len(trend.series) == 1
    assert trend.series[0].basis_label == "同一机构：合成检验中心"


@pytest.mark.parametrize(
    ("first_overrides", "second_overrides"),
    [
        ({}, {"result_type": ResultType.COMPARATOR}),
        ({"precision": DatePrecision.MONTH}, {"precision": DatePrecision.MONTH}),
        ({"raw_unit": "mg/L"}, {"raw_unit": "mmol/L"}),
        ({"method": "方法A"}, {"method": "方法B"}),
        ({"method": "", "institution": "机构A"}, {"method": "", "institution": "机构B"}),
        ({"capability": CapabilityLevel.SEARCH_ONLY}, {"capability": CapabilityLevel.SEARCH_ONLY}),
    ],
)
def test_ineligible_combinations_have_no_entry_and_return_not_found(
    django_user_model, first_overrides, second_overrides
):
    client, patient = _patient(django_user_model, uuid.uuid4().hex[0])
    first_document, _first = _observation(patient, date(2026, 7, 1), "4.2", **first_overrides)
    _observation(patient, date(2026, 8, 1), "4.6", **second_overrides)

    assert "LAB_WBC" not in eligible_trend_codes(patient, ("LAB_WBC",))
    assert client.get("/trends/LAB_WBC/").status_code == 404
    assert "/trends/LAB_WBC/" not in client.get(f"/records/{first_document.pk}/").content.decode()


def test_conflicting_units_form_separate_series_without_conversion_or_forced_connection(django_user_model):
    client, patient = _patient(django_user_model, "v")
    _observation(patient, date(2026, 7, 1), "1.00", raw_unit="mg/L")
    _observation(patient, date(2026, 8, 1), "2.00", raw_unit="mg/L")
    _observation(patient, date(2026, 7, 2), "0.10", raw_unit="mmol/L")
    _observation(patient, date(2026, 8, 2), "0.20", raw_unit="mmol/L")

    response = client.get("/trends/LAB_WBC/")
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count('class="trend-series"') == 2
    assert content.count("<polyline") == 2
    assert "1.00" in content and "0.10" in content
    assert "系统不做单位换算" in content


def test_trend_never_uses_another_patients_points(django_user_model):
    client, patient = _patient(django_user_model, "w")
    _other_client, other = _patient(django_user_model, "x")
    _observation(patient, date(2026, 7, 1), "4.2")
    _observation(other, date(2026, 7, 1), "99.1")
    _observation(other, date(2026, 8, 1), "99.2")

    response = client.get("/trends/LAB_WBC/")

    assert response.status_code == 404
    assert b"99.1" not in response.content and b"99.2" not in response.content
