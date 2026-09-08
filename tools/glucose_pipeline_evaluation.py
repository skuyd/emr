"""Replay all frozen OCR inputs through real persistence and the glucose adapter.

Outputs contain private source text and must stay in a new local .runtime folder.
Prediction accepts only the fixed input manifest and the real dictionary. Gold,
protocol and evidence are passed solely to the separately reviewed scorer after
predictions.json has been written. This does not call a new OCR service.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


class EvaluationInputError(ValueError):
    pass


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_manifest(manifest):
    """Verify the entire inventory before creating any database records."""
    sources = manifest.get('sources', [])
    if not sources:
        raise EvaluationInputError('The fixed input manifest is empty')
    numbers, identities, roots, pages_by_hash = set(), set(), set(), {}
    for source in sources:
        number, identity = source.get('source_number'), source.get('source_sha256')
        count = source.get('ocr_pages')
        if (type(number) is not int or number < 1 or number in numbers
                or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)
                or identity in identities or type(count) is not int or count < 1):
            raise EvaluationInputError('Duplicate or invalid source/page inventory')
        numbers.add(number)
        identities.add(identity)
        for path_key, hash_key in [('source_path', 'source_sha256'), ('ocr_path', 'ocr_sha256')]:
            expected = source.get(hash_key)
            if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                    or file_digest(source[path_key]) != expected):
                raise EvaluationInputError('Frozen original or OCR bytes changed')
        cache = Path(source['ocr_path']).resolve()
        roots.add(cache.parent)
        if cache.name != identity + '.json':
            raise EvaluationInputError('Frozen cache path does not match the source identity')
        content = json.loads(cache.read_text(encoding='utf-8'))
        pages = content.get('pages', [])
        if (content.get('source_file_hash') != identity or len(pages) != count
                or [p.get('page_number') for p in pages] != list(range(1, count + 1))):
            raise EvaluationInputError('Frozen cache source/page sequence changed')
        pages_by_hash[identity] = pages
    if len(roots) != 1:
        raise EvaluationInputError('Frozen caches must share one unchanged root')
    return pages_by_hash


def verify_coverage(manifest, coverage):
    expected = {(s['source_sha256'], s['ocr_sha256'], page)
                for s in manifest['sources'] for page in range(1, s['ocr_pages'] + 1)}
    actual = [(p.get('source_sha256'), p.get('fixed_ocr_sha256'), p.get('manifest_page'))
              for p in coverage.get('pages', [])]
    if len(actual) != len(expected) or set(actual) != expected:
        raise EvaluationInputError('Coverage must include every fixed input page exactly once')


def predict_sources(manifest, dictionary, *, progress=None):
    """Run the actual app on all sources, independently of evaluation answers."""
    from django.db import connection
    from apps.documents.models import Document
    from tools.glucose_source_mapping import map_pipeline_document
    from tools.phase_two_evaluation import predict_frozen_sources

    if (connection.vendor != 'sqlite' or str(connection.settings_dict['NAME']) not in {
            ':memory:', 'file:memorydb_default?mode=memory&cache=shared'} or Document.objects.exists()):
        raise EvaluationInputError('Prediction requires an empty in-memory evaluation database')
    raw_pages = verify_manifest(manifest)
    sources = manifest['sources']
    baseline = {'samples': [{'source_file_hash': s['source_sha256'], 'ocr_cache_sha256': s['ocr_sha256'],
                            'ocr_pages': s['ocr_pages']} for s in sources]}
    # This source-level task has no cross-document patient linkage. Each source
    # gets an isolated synthetic patient, without using any gold patient labels.
    classification = {'files': [{'source_file_hash': s['source_sha256'], 'patient_group_id': s['source_sha256']}
                                for s in sources]}
    replay = predict_frozen_sources([{'hash': s['source_sha256'], 'path': s['source_path']} for s in sources],
        Path(sources[0]['ocr_path']).parent, baseline, classification, dictionary, progress=progress)
    pipeline_results = {row['source_file_hash']: row for row in replay['predictions']}
    documents = {document.sha256: document for document in Document.objects.all()}
    if set(documents) != set(raw_pages) or set(pipeline_results) != set(raw_pages):
        raise EvaluationInputError('Actual persisted source inventory differs from the fixed manifest')
    predictions = {'schema_version': 1, 'pages': [], 'unattributable_items': [], 'document_receipts': []}
    for source in sources:
        identity = source['source_sha256']
        pipeline_state = pipeline_results[identity]['status']
        state = 'COMPLETE' if pipeline_state in {'success', 'original_only'} else 'FAILED'
        mapped = map_pipeline_document(documents[identity], raw_pages[identity], source_sha256=identity,
            fixed_ocr_sha256=source['ocr_sha256'],
            execution_status={page: state for page in range(1, source['ocr_pages'] + 1)})
        predictions['pages'].extend(mapped['pages'])
        predictions['unattributable_items'].extend(mapped['unattributable_items'])
        predictions['document_receipts'].append({'source_sha256': identity, 'source_number': source['source_number'],
            'pipeline_status': pipeline_state, 'pipeline_failure_state': pipeline_results[identity].get('failure_state'),
            **mapped['mapping_receipt']})
    return predictions, {**deepcopy(replay['execution']), 'fixed_source_count': len(sources),
        'fixed_page_count': len(predictions['pages']),
        'failed_pipeline_sources': sum(row['status'] == 'failed' for row in pipeline_results.values()),
        'patient_grouping': 'One isolated synthetic patient per source; no cross-document identity inference.'}


def application_identity(root):
    """Capture exact checked-in files and reject an unfrozen working tree."""
    command = lambda *args: subprocess.check_output(['git', '-C', str(root), *args])
    if command('status', '--porcelain', '--untracked-files=normal').strip():
        raise EvaluationInputError('Commit and independently review the application before real execution')
    names = command('ls-files', '-z').decode('utf-8').split('\0')
    files = {name: file_digest(root / name) for name in names if name}
    return {'head': command('rev-parse', 'HEAD').decode().strip(), 'tracked_files': files}


def main(argv=None):
    import argparse
    import os
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    input_names = ('manifest', 'gold', 'protocol', 'evidence', 'coverage')
    for name in input_names:
        parser.add_argument('--' + name, type=Path, required=True)
        parser.add_argument('--' + name + '-sha256', required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--dictionary-version', required=True)
    parser.add_argument('--dictionary-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)

    def verify_input_files():
        for name in input_names:
            if file_digest(getattr(args, name)) != getattr(args, name + '_sha256'):
                raise EvaluationInputError('Frozen scoring input identity changed: ' + name)

    verify_input_files()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    coverage = json.loads(args.coverage.read_text(encoding='utf-8'))
    verify_manifest(manifest)
    verify_coverage(manifest, coverage)
    application = application_identity(root)
    if application['head'] != args.expected_head:
        raise EvaluationInputError('Reviewed application commit changed')
    output = args.output.resolve()
    if not output.is_relative_to((root / '.runtime').resolve()) or output.exists():
        raise EvaluationInputError('Use a new private .runtime directory; previous evidence is immutable')
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
    import django
    django.setup()
    from django.core.management import call_command
    from apps.labs.dictionary import current_dictionary
    from tools.phase_two_evaluation import capture_parser_identity
    from tools.glucose_source_evaluation import score_predictions

    call_command('migrate', verbosity=0)
    dictionary = current_dictionary()
    if dictionary.version != args.dictionary_version or dictionary.content_hash != args.dictionary_sha256:
        raise EvaluationInputError('Reviewed dictionary identity changed')
    parser_identity = capture_parser_identity(root)
    identity = {'schema_version': 1, 'frozen_at': datetime.now(timezone.utc).isoformat(), 'prediction_seen': False,
        'inputs': {name: getattr(args, name + '_sha256') for name in input_names},
        'application': application, 'parser': parser_identity,
        'dictionary_version': dictionary.version, 'dictionary_hash': dictionary.content_hash,
        'original_and_ocr_hashes_verified': 2 * len(manifest['sources']),
        'source_count': len(manifest['sources']), 'page_count': sum(s['ocr_pages'] for s in manifest['sources'])}
    output.mkdir(parents=True)

    def write(name, value):
        with (output / name).open('xb') as handle:
            handle.write((json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n').encode('utf-8'))

    write('input-freeze.json', identity)
    phase = 'prediction'
    try:
        predictions, execution = predict_sources(manifest, dictionary,
            progress=lambda done, total, status: print(f'{done}/{total} {status}', flush=True))
        write('predictions.json', predictions)
        phase = 'post-prediction-identity-check'
        verify_input_files()
        verify_manifest(manifest)
        if application_identity(root) != application or capture_parser_identity(root) != parser_identity:
            raise EvaluationInputError('Application or runtime changed during frozen prediction')
        phase = 'scoring'
        # Expected answers enter only here, after predictions have been preserved.
        result = score_predictions(*(json.loads(getattr(args, name).read_text(encoding='utf-8'))
                                     for name in ('gold', 'protocol', 'evidence', 'coverage')), predictions)
        write('private-matching-trace.json', result['details'])
        write('public-report.json', {'schema_version': 1, 'summary': result['summary'],
            'evidence_identity': {name: file_digest(output / name) for name in (
                'input-freeze.json', 'predictions.json', 'private-matching-trace.json')},
            'input_identity': identity['inputs'], 'application_head': application['head'],
            'dictionary_version': dictionary.version, 'dictionary_hash': dictionary.content_hash,
            'execution': {k: v for k, v in execution.items() if k != 'files'},
            'scope': 'Authorized fixed development corpus; no independent holdout or aggregate clinical accuracy.'})
        print(json.dumps({'source_rows': result['summary']['source_rows'],
                          'execution': result['summary']['execution']}), flush=True)
    except Exception as error:
        write('execution-failure.json', {'phase': phase, 'error_type': type(error).__name__, 'message': str(error)})
        raise


if __name__ == '__main__':
    main()
