"""Geometry is evaluated at actual persistence and source-viewer boundaries."""
from dataclasses import replace
import io

from django.urls import reverse
import pytest

from apps.labs.models import LabObservation
from apps.facts.models import Fact
from apps.patients.services import create_patient_space
from apps.processing.images import prepare_image
from apps.processing.models import OcrBlock, ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.processing.test_pipeline import _Store, _document_and_run, _ocr_page, _png_bytes


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("transform", [((.6, 0., .2), (0., .6, .1), (0., 0., 1.)), None])
def test_pipeline_saves_original_sources_and_separate_layout_without_fabricating_fallback(django_user_model, monkeypatch, client, transform):
    document, run = _document_and_run(django_user_model)
    payload = _png_bytes()
    prepared = prepare_image(io.BytesIO(payload), "image/png")
    prepared.pages = (replace(prepared.pages[0], source_transform=transform,
                             preparation_metadata={"version": "synthetic", "steps": ["paper_rectified"]}),)
    monkeypatch.setattr("apps.processing.pipeline.prepare_document", lambda *_: prepared)
    ocr = _ocr_page()
    ocr = replace(ocr, regions=ocr.regions + (OcrRegion("临床诊断：合成诊断", ((.1,.5),(.4,.5),(.4,.55),(.1,.55)), .98, 7),))
    pipeline = DocumentProcessingPipeline(object_store=_Store(payload), raster_provider=FixtureOcrProvider((ocr,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    block = OcrBlock.objects.get(parsing_version=version, reading_order=3)
    observation = LabObservation.objects.get(parsing_version=version)
    fact = Fact.objects.get(parsing_version=version)
    for actual, expected in zip(block.layout_polygon, [[.05, .2], [.3, .2], [.3, .24], [.05, .24]], strict=True):
        assert actual == pytest.approx(expected)
    assert version.diagnostics["preparation_pages"]["1"]["version"] == "synthetic"
    if transform:
        assert block.polygon[0] == pytest.approx([.23, .22])
        assert observation.evidence.polygon[0] == pytest.approx([.23, .22])
        assert observation.evidence.polygon[2] == pytest.approx([.74, .244])
        assert observation.field_evidence["raw_unit"]["polygon"][0] == pytest.approx([.53, .22])
        candidate = version.metadata_candidates.filter(kind="DOCUMENT_DATE", selected=True).first()
        assert candidate.evidence.polygon[0] == pytest.approx([.23, .16])
        assert fact.evidence.polygon[0] == pytest.approx([.26, .4])
    else:
        assert block.polygon is None
        assert observation.evidence.polygon is None
        assert fact.evidence.polygon is None
        assert all(value["polygon"] is None and value["precision"] == "page" for value in observation.field_evidence.values())
    assert document.original_object_key == pipeline.object_store.keys[0]
    account = document.patient.account
    create_patient_space(account, "合成患者", {"privacy": True, "sensitive_data": True, "upload_authority": True},
                         {"ip": "127.0.0.1", "user_agent": "synthetic-geometry-test"})
    client.force_login(account)
    response = client.get(reverse("documents:document_viewer", args=(document.pk,)), {"evidence": observation.evidence_id})
    assert response.status_code == 200
    assert response.context["highlight_rect"] == ("23.0000,22.0000,51.0000,2.4000" if transform else "")


def test_unmappable_or_padded_geometry_never_becomes_an_original_highlight():
    from apps.processing.geometry import source_polygon
    polygon = ((0., 0.), (.2, 0.), (.2, .1), (0., .1))
    assert source_polygon(((1., 0., -.1), (0., 1., 0.), (0., 0., 1.)), polygon) is None
    assert source_polygon(((0., 0., 0.), (0., 0., 0.), (0., 0., 0.)), polygon) is None
    assert source_polygon(None, polygon) is None


def test_fact_lines_use_layout_geometry_while_returning_original_source_blocks():
    from types import SimpleNamespace
    from apps.facts.layout import source_lines
    first = SimpleNamespace(document_page_id="1", text="临床诊断：", polygon=[[.1,.1],[.25,.14],[.25,.16],[.1,.12]],
                            layout_polygon=[[.1,.1],[.25,.1],[.25,.12],[.1,.12]])
    second = SimpleNamespace(document_page_id="1", text="合成诊断", polygon=[[.26,.2],[.4,.24],[.4,.26],[.26,.22]],
                             layout_polygon=[[.26,.1],[.4,.1],[.4,.12],[.26,.12]])
    rows = list(source_lines([first, second]))
    assert len(rows) == 1
    assert rows[0][1] == "临床诊断： 合成诊断"
    assert rows[0][2] == [first, second]


def test_fact_section_lane_uses_layout_coordinates_without_absorbing_side_column():
    from types import SimpleNamespace
    from apps.facts.extraction import section_candidates
    from apps.processing.geometry import source_polygon
    transform = ((.5, 0., .2), (0., .5, .2), (0., 0., 1.))

    def block(text, left, right, top):
        layout = ((left, top), (right, top), (right, top+.02), (left, top+.02))
        return SimpleNamespace(document_page_id="1", text=text, polygon=source_polygon(transform, layout),
                               layout_polygon=layout)

    heading = block("临床诊断：", .1, .3, .1)
    side_date = block("报告日期：2026-08-20", .6, .9, .2)
    continuation = block("合成诊断内容", .1, .3, .3)
    sections = section_candidates([heading, side_date, continuation], "DISCHARGE")
    assert len(sections) == 1
    assert sections[0]["lines"] == [heading.text, continuation.text]
    assert sections[0]["blocks"] == [heading, continuation]
