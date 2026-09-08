"""Synthetic fixed inputs through the actual isolated pipeline and mapper."""
from copy import deepcopy
import hashlib
import json

import pytest

from tools.glucose_pipeline_evaluation import EvaluationInputError, predict_sources, verify_manifest, verify_coverage
from tests.tools.test_glucose_source_mapping import synthetic


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixed_manifest(tmp_path):
    sources = []
    for number in (1, 2):
        original = tmp_path / f'synthetic-{number}.bin'
        original.write_bytes(f'synthetic-original-{number}'.encode())
        source_sha = digest(original)
        pages, _blocks, _candidate = synthetic(unit='mmo1/L')
        page = pages[0]
        page.update(provider='synthetic', provider_version='1', provider_metadata={})
        if number == 1:
            page['regions'] = [r for r in page['regions'] if r['text'] != 'GLU']
            pages.append({**deepcopy(page), 'page_number': 2, 'regions': []})
        cache = tmp_path / (source_sha + '.json')
        cache.write_text(json.dumps({'source_file_hash': source_sha, 'pages': pages}, ensure_ascii=False), encoding='utf-8')
        sources.append({'source_number': number, 'source_path': str(original), 'source_sha256': source_sha,
                        'ocr_path': str(cache), 'ocr_sha256': digest(cache), 'ocr_pages': len(pages)})
    return {'sources': sources}


@pytest.mark.django_db(transaction=True)
def test_real_pipeline_preserves_all_pages_candidates_and_current_split_code_exclusion(tmp_path):
    from apps.documents.models import Document
    from apps.labs.dictionary import phase_two_dictionary
    from apps.patients.models import Patient
    manifest = fixed_manifest(tmp_path)
    before = deepcopy(manifest)
    predictions, execution = predict_sources(manifest, phase_two_dictionary())
    assert manifest == before and Document.objects.count() == Patient.objects.count() == 2
    assert len(predictions['pages']) == 3
    assert [p['status'] for p in predictions['pages']] == ['COMPLETE'] * 3
    first, blank, excluded = predictions['pages']
    assert first['items'][0]['disposition'] == 'ADMITTED'
    assert first['items'][0]['values']['raw_unit'] == 'mmo1/L'
    assert first['items'][0]['values']['decimal_value'] == '5.50'
    assert first['items'][0]['values']['sampling']['local_datetime'] == '2024-04-01T07:08:09'
    assert blank['items'] == blank['input_blocks'] == []
    assert excluded['items'][0]['disposition'] == 'EXCLUDED'
    assert excluded['items'][0]['mapping_receipt']['diagnostic']['effective_raw_name'] == 'GLU 葡萄糖'
    assert execution['fixed_source_count'] == 2 and execution['fixed_page_count'] == 3
    assert execution['failed_pipeline_sources'] == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('key', ['source_path', 'ocr_path'])
def test_changed_input_bytes_abort_before_any_actual_database_write(tmp_path, key):
    from apps.documents.models import Document
    from apps.labs.dictionary import phase_two_dictionary
    from pathlib import Path
    manifest = fixed_manifest(tmp_path)
    with Path(manifest['sources'][-1][key]).open('ab') as handle:
        handle.write(b'changed')
    with pytest.raises(EvaluationInputError):
        predict_sources(manifest, phase_two_dictionary())
    assert not Document.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_real_persistence_failure_retains_failed_source_pages(tmp_path, monkeypatch):
    from apps.labs.dictionary import phase_two_dictionary
    from apps.processing.pipeline import DocumentProcessingPipeline
    manifest = fixed_manifest(tmp_path)
    persist = DocumentProcessingPipeline._persist

    def fail_second(self, context, document, *args, **kwargs):
        if document.sha256 == manifest['sources'][1]['source_sha256']:
            raise RuntimeError('synthetic-persistence-failure')
        return persist(self, context, document, *args, **kwargs)

    monkeypatch.setattr(DocumentProcessingPipeline, '_persist', fail_second)
    result, execution = predict_sources(manifest, phase_two_dictionary())
    assert [p['status'] for p in result['pages']] == ['COMPLETE', 'COMPLETE', 'FAILED']
    assert result['pages'][2]['input_blocks']
    assert result['pages'][2]['pipeline_status'] == 'FAILED'
    assert execution['failed_pipeline_sources'] == 1


@pytest.mark.django_db(transaction=True)
def test_prediction_refuses_a_database_with_existing_documents(tmp_path, django_user_model):
    from apps.labs.dictionary import phase_two_dictionary
    from tests.glucose.factories import lab_source
    lab_source(django_user_model)
    with pytest.raises(EvaluationInputError, match='empty'):
        predict_sources(fixed_manifest(tmp_path), phase_two_dictionary())


@pytest.mark.parametrize('change', ['duplicate_source', 'duplicate_number', 'wrong_page_count'])
def test_manifest_never_silently_narrows_duplicate_or_changed_inventory(tmp_path, change):
    manifest = fixed_manifest(tmp_path)
    if change == 'duplicate_source':
        manifest['sources'].append(deepcopy(manifest['sources'][0]))
    elif change == 'duplicate_number':
        manifest['sources'][1]['source_number'] = 1
    else:
        manifest['sources'][0]['ocr_pages'] += 1
    with pytest.raises(EvaluationInputError):
        verify_manifest(manifest)


def test_coverage_must_include_every_source_and_page_once(tmp_path):
    manifest = fixed_manifest(tmp_path)
    coverage = {'pages': [{'source_sha256': source['source_sha256'], 'fixed_ocr_sha256': source['ocr_sha256'],
                          'manifest_page': page}
                         for source in manifest['sources'] for page in range(1, source['ocr_pages'] + 1)]}
    verify_coverage(manifest, coverage)
    for pages in (coverage['pages'][:-1], coverage['pages'] + coverage['pages'][:1]):
        with pytest.raises(EvaluationInputError):
            verify_coverage(manifest, {'pages': pages})
