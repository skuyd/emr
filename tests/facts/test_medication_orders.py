from types import SimpleNamespace

import pytest

from apps.facts.extraction import section_candidates


def cell(text, x, y, width=.06, *, page="page-1"):
    return SimpleNamespace(
        text=text, document_page_id=page, reading_order=0,
        polygon=[[x, y], [x + width, y], [x + width, y + .016], [x, y + .016]],
    )


def table_headers(*, page="page-1"):
    return [
        cell("开始时间", .01, .10, page=page),
        cell("医嘱类别", .09, .10, page=page),
        cell("医嘱类型", .16, .10, page=page),
        cell("医嘱内容", .24, .10, page=page),
        cell("执行科室", .51, .10, page=page),
        cell("执行状态", .59, .10, page=page),
        cell("开嘱医生", .67, .10, page=page),
        cell("停止医嘱", .77, .085, .18, page=page),
        cell("时间", .78, .11, .04, page=page),
        cell("医生签名", .88, .11, page=page),
    ]


def order(y, *, page="page-1", stopped=False):
    return [
        cell("2026-08-01 09:30", .01, y, .07, page=page),
        cell("药疗", .09, y, .04, page=page),
        cell("长期" if stopped else "临时", .16, y, .04, page=page),
        cell("[已停止]合成药甲 2片 口服 BID" if stopped else "[已作废]合成药乙 1g 静脉滴注 ST",
             .24, y, .25, page=page),
        cell("合成科室", .51, y, .06, page=page),
        cell("已执行" if stopped else "已作废", .59, y, .06, page=page),
        cell("合成医生", .67, y, .06, page=page),
        *([cell("2026-08-02 10:00", .77, y, .08, page=page)] if stopped else []),
    ]


def test_medication_table_yields_complete_separate_rows_with_status_and_dates():
    items = table_headers() + order(.20, stopped=True) + order(.24)
    # OCR order need not equal visual column order.
    items[10:] = reversed(items[10:])
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 2
    first, second = ("\n".join(row["lines"]) for row in rows)
    assert "开始时间：2026-08-01 09:30" in first
    assert "长期 [已停止]合成药甲 2片 口服 BID" in first
    assert "执行状态：已执行" in first
    assert "停止医嘱：2026-08-02 10:00" in first
    assert "临时 [已作废]合成药乙 1g 静脉滴注 ST" in second
    assert "执行状态：已作废" in second
    assert "停止医嘱：" not in second
    assert all(row["category"] == "TREATMENT" and row["page"] == "page-1" for row in rows)
    assert all("合成医生" not in "\n".join(row["lines"]) for row in rows)
    assert rows[0]["record_date"]["value"] is None


def test_medication_rows_need_real_table_headers_and_do_not_inherit_other_page_headers():
    assert section_candidates(order(.20), "PRESCRIPTION") == []
    items = table_headers() + order(.20) + order(.20, page="page-2")
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1 and rows[0]["page"] == "page-1"


def test_medication_table_keeps_same_time_duplicates_and_rejects_clipped_start_date():
    partial = order(.28)
    partial[0].text = "08-01 09:"
    rows = section_candidates(table_headers() + order(.20) + order(.24) + partial, "UNKNOWN")
    assert len(rows) == 2
    assert rows[0]["blocks"] != rows[1]["blocks"]


def test_medication_content_in_several_boxes_retains_dose_route_and_frequency():
    items = table_headers() + order(.20)
    content = items[13]
    content.text = "[已作废]合成药乙"
    content.polygon = [[.24, .20], [.35, .20], [.35, .216], [.24, .216]]
    items.extend([cell("1g", .36, .20, .025), cell("静脉滴注", .40, .20, .055),
                  cell("ST", .465, .20, .025)])
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1
    text = "\n".join(rows[0]["lines"])
    assert all(value in text for value in ("[已作废]合成药乙", "1g", "静脉滴注", "ST"))


def test_non_medication_order_between_drugs_never_enters_a_drug_excerpt():
    other = order(.24)
    other[1].text = "护理"
    other[3].text = "合成护理项目"
    rows = section_candidates(table_headers() + order(.20) + other + order(.28), "UNKNOWN")
    assert len(rows) == 2
    assert all("合成护理项目" not in "\n".join(row["lines"]) for row in rows)


@pytest.mark.parametrize("label", ["开立医生", "签名医生"])
def test_opening_doctor_column_is_not_part_of_execution_status(label):
    headers = table_headers()
    headers[6].text = label
    rows = section_candidates(headers + order(.20), "UNKNOWN")
    assert len(rows) == 1
    assert rows[0]["lines"][2] == "执行状态：已作废"


def test_stop_order_time_is_not_replaced_by_later_nursing_acknowledgement():
    headers = table_headers()[:7] + [
        cell("核对医嘱", .61, .085, .07),
        cell("时间", .61, .11, .04),
        cell("护士签名", .66, .11, .055),
        cell("停止医嘱", .77, .085, .08),
        cell("时间", .71, .11, .04),
        cell("医生签名", .765, .11, .055),
        cell("时间", .83, .11, .04),
        cell("护士签名", .89, .11, .055),
    ]
    items = headers + order(.20) + [
        cell("2026-08-02 10:00", .70, .20, .065),
        cell("2026-08-02 10:15", .82, .20, .065),
    ]
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1
    assert rows[0]["lines"][-1] == "停止医嘱：2026-08-02 10:00"


def test_explicit_stop_time_column_does_not_borrow_another_time_column():
    headers = table_headers()[:7] + [
        cell("时间", .73, .10, .04), cell("停止时间", .84, .10, .07),
    ]
    items = headers + order(.20) + [
        cell("2026-08-01 10:00", .71, .20, .08),
        cell("2026-08-02 11:00", .83, .20, .08),
    ]
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1
    assert rows[0]["lines"][-1] == "停止时间：2026-08-02 11:00"


def test_first_order_does_not_absorb_a_stacked_time_header():
    items = table_headers() + order(.125)
    anchor = items[11]
    anchor.polygon = [[.09, .12], [.13, .12], [.13, .15], [.09, .15]]
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1
    assert not any(line.startswith("停止医嘱：") for line in rows[0]["lines"])


@pytest.mark.parametrize("overlap", [True, False])
def test_overlapping_ocr_text_is_joined_once_without_erasing_adjacent_repeated_doses(overlap):
    items = table_headers() + order(.20)
    content = items[13]
    content.text = "合成药甲2"
    content.polygon = [[.24, .20], [.34, .20], [.34, .216], [.24, .216]]
    items.append(cell("2片口服BID", .325 if overlap else .35, .20, .12))
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1
    text = "".join(rows[0]["lines"][1].split())
    assert text == ("临时合成药甲2片口服BID" if overlap else "临时合成药甲22片口服BID")


def test_order_near_page_edge_is_explicitly_bounded_without_completing_clipped_text():
    items = table_headers() + order(.98)
    rows = section_candidates(items, "UNKNOWN")
    assert len(rows) == 1
    assert rows[0]["page_bounded"]
    assert rows[0]["lines"][1] == "临时 [已作废]合成药乙 1g 静脉滴注 ST"


def test_overlap_uses_layout_coordinates_but_audits_original_source_fragments():
    boxes = table_headers() + order(.20)
    boxes[13].text = "合成药甲2"
    boxes[13].polygon = [[.24, .20], [.34, .20], [.34, .216], [.24, .216]]
    boxes.append(cell("2片口服BID", .325, .20, .12))
    for box in boxes:
        box.layout_polygon = box.polygon
        box.polygon = [[1 - y, x] for x, y in box.polygon]
    row, = section_candidates(boxes, "UNKNOWN")
    assert "合成药甲2片口服BID" in "".join(row["lines"][1].split())
    trace, = row["transcription_transforms"]
    source_polygon = [list(point) for point in trace["source_fragments"][0]["polygon"]]
    assert source_polygon == boxes[13].polygon
    assert source_polygon != boxes[13].layout_polygon


@pytest.mark.django_db
def test_medication_candidates_keep_original_sources_and_need_review_before_export(django_user_model):
    from django.utils import timezone
    from apps.documents.models import ProcessingRun, ProcessingStage
    from apps.exports.content import build_snapshot
    from apps.facts.extraction import extract_version_facts
    from apps.facts.models import Fact
    from apps.facts.readmodels import usable_facts
    from apps.facts.revisions import revise_fact
    from apps.processing.models import DocumentSummary, OcrBlock, ParsingVersion, ParsingVersionStatus
    from tests.documents.test_detail_viewer import _document, _patient

    client, patient = _patient(django_user_model, "synthetic-order-review")
    document, pages = _document(patient)
    run = ProcessingRun.objects.create(
        document=document, parser_version="synthetic-orders", idempotency_key=str(document.pk),
        stage=ProcessingStage.SUCCEEDED, finished_at=timezone.now(),
    )
    version = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="synthetic-orders", status=ParsingVersionStatus.READY,
    )
    DocumentSummary.objects.create(parsing_version=version, document_type="UNKNOWN", confidence=".99")
    for index, box in enumerate(table_headers() + order(.20, stopped=True)):
        OcrBlock.objects.create(
            parsing_version=version, document_page=pages[0], reading_order=index,
            text=box.text, polygon=box.polygon, confidence=".99",
        )
    extract_version_facts(version)
    ParsingVersion.objects.activate(version)
    fact = Fact.objects.get(parsing_version=version)
    assert fact.evidence.source_text == fact.raw_text
    assert fact.evidence.document_page_id == pages[0].pk
    assert fact.evidence.polygon is None
    assert fact.automatic_content["record_date"]["value"] is None
    assert [value["value"] for value in fact.automatic_content["dates"]] == ["2026-08-01", "2026-08-02"]
    assert fact.automatic_content["date"] is None
    assert usable_facts(patient) == ()
    assert "合成药甲" in client.get(f"/facts/documents/{document.pk}/").content.decode()
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)]})
    assert snapshot["facts"][0]["content"]["text"] == fact.raw_text
    assert "停止医嘱：2026-08-02 10:00" in snapshot["facts"][0]["content"]["text"]
    revise_fact(patient, fact.pk, action="REVOKE", expected_revision=1)
    assert usable_facts(patient) == ()


@pytest.mark.django_db
@pytest.mark.parametrize("overlap", [True, False])
def test_overlap_audit_survives_persistence_review_and_correction_and_is_visible(django_user_model, overlap):
    from copy import deepcopy
    from django.utils import timezone
    from apps.documents.models import ProcessingRun, ProcessingStage
    from apps.facts.extraction import extract_version_facts
    from apps.facts.models import Fact
    from apps.facts.revisions import revise_fact
    from apps.processing.models import DocumentSummary, OcrBlock, ParsingVersion, ParsingVersionStatus
    from tests.documents.test_detail_viewer import _document, _patient

    client, patient = _patient(django_user_model, "synthetic-overlap-audit")
    document, pages = _document(patient)
    run = ProcessingRun.objects.create(
        document=document, parser_version="synthetic-overlap", idempotency_key=str(document.pk),
        stage=ProcessingStage.SUCCEEDED, finished_at=timezone.now(),
    )
    version = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="synthetic-overlap", status=ParsingVersionStatus.READY,
    )
    DocumentSummary.objects.create(parsing_version=version, document_type="UNKNOWN", confidence=".99")
    boxes = table_headers() + order(.20)
    boxes[13].text = "合成药甲2"
    boxes[13].polygon = [[.24, .20], [.34, .20], [.34, .216], [.24, .216]]
    boxes.append(cell("2片口服BID", .325 if overlap else .35, .20, .12))
    for index, box in enumerate(boxes):
        OcrBlock.objects.create(
            parsing_version=version, document_page=pages[0], reading_order=index,
            text=box.text, polygon=box.polygon, confidence=".99",
        )
    extract_version_facts(version)
    ParsingVersion.objects.activate(version)
    fact = Fact.objects.get(parsing_version=version)
    automatic = deepcopy(fact.automatic_content)
    if overlap:
        trace, = automatic["transcription_transforms"]
        assert trace["before"] == "合成药甲2 2片口服BID"
        assert trace["after"] == "合成药甲2 片口服BID"
        assert trace["overlapping_text"] == "2"
        assert trace["rule_version"] and "重叠" in trace["reason"]
        assert [piece["text"] for piece in trace["source_fragments"]] == ["合成药甲2", "2片口服BID"]
        assert trace["source_fragments"][0]["polygon"] == boxes[13].polygon
        assert trace["source_fragments"][0]["reading_order"] == 13
    else:
        assert "transcription_transforms" not in automatic
        assert "合成药甲22片口服BID" in "".join(fact.raw_text.split())
    for path in (f"/facts/documents/{document.pk}/", f"/facts/{fact.pk}/"):
        response = client.get(path)
        assert response.status_code == 200
        rendered = response.content.decode()
        assert ("重叠文字已合并，请核对剂量" in rendered) is overlap
        if overlap:
            assert automatic["transcription_transforms"][0]["before"] in rendered
            assert "查看原始片段和合并过程" in rendered
    confirmed = revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    corrected = revise_fact(patient, fact.pk, action="CORRECT", expected_revision=1, checked_original=True,
                            changes={"text": "对照原件更正的合成药物摘录"})
    fact.refresh_from_db()
    assert fact.automatic_content == automatic
    assert confirmed.after["content"].get("transcription_transforms") == automatic.get("transcription_transforms")
    assert corrected.before["content"].get("transcription_transforms") == automatic.get("transcription_transforms")
    assert corrected.after["content"].get("transcription_transforms") == automatic.get("transcription_transforms")
    assert ("重叠文字已合并，请核对剂量" in client.get(f"/facts/{fact.pk}/").content.decode()) is overlap
