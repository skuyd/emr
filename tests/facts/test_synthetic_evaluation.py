import json
import os
from pathlib import Path

import pytest

from apps.facts.extraction import EXTRACTOR_VERSION, extract_version_facts
from apps.facts.models import Fact, FactExtraction
from apps.processing.models import DocumentSummary, OcrBlock, ParsingVersion
from apps.documents.models import ProcessingRun
from tests.documents.test_detail_viewer import _document, _patient
from tools.phase_three_evaluation import evaluate_predictions, file_digest


@pytest.mark.django_db
def test_frozen_synthetic_fact_corpus(django_user_model):
    corpus_path = Path("tests/fixtures/facts/synthetic-corpus.json")
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    _, patient = _patient(django_user_model, "synthetic-fact-evaluation")
    annotations, predictions = [], []
    for number, case in enumerate(corpus["cases"], 1):
        document, pages = _document(patient, page_count=len(case["pages"]))
        run = ProcessingRun.objects.create(document=document, parser_version="synthetic-facts",
                                            idempotency_key=str(document.pk))
        version = ParsingVersion.objects.create(document=document, processing_run=run, parser_version="synthetic-facts")
        DocumentSummary.objects.create(parsing_version=version, document_type=case["document_type"], confidence=".99")
        for page, text in zip(pages, case["pages"], strict=True):
            OcrBlock.objects.create(parsing_version=version, document_page=page, reading_order=0, text=text,
                polygon=[[.1,.1],[.8,.1],[.8,.8],[.1,.8]], confidence=".99")
        extract_version_facts(version)
        expected = case["expected"]
        actual = list(Fact.objects.filter(parsing_version=version).select_related("document_page", "evidence").order_by("reading_order"))
        annotations.append(dict(source_number=number, reviewed_pages=list(range(1,len(pages)+1)), facts=expected))
        predictions.append(dict(source_number=number, status=FactExtraction.objects.get(parsing_version=version).status,
            facts=[dict(category=fact.category, page=fact.document_page.page_number, text=fact.raw_text) for fact in actual]))
        single = evaluate_predictions(annotations[-1:], predictions[-1:])
        assert single["fields"]["correct"] == len(expected), case["id"]
        assert single["fields"]["extra"] == 0, case["id"]
        for fact, gold in zip(actual, expected, strict=True):
            assert fact.evidence.document_page_id == fact.document_page_id
            assert fact.evidence.source_text == fact.raw_text
            if "event_date" in gold:
                assert fact.automatic_content["date"] == gold["event_date"], case["id"]
                assert fact.automatic_content["date_precision"] == gold["event_precision"], case["id"]
    report = dict(schema_version=1, scope=corpus["scope"], corpus_sha256=file_digest(corpus_path),
                  extractor_version=EXTRACTOR_VERSION, current=evaluate_predictions(annotations,predictions),
                  limitations=["Synthetic fixture contracts only; not real-document accuracy or OCR quality."])
    destination = os.environ.get("PHR_FACT_SYNTHETIC_REPORT")
    if destination:
        target = Path(destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
