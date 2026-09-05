"""Replay the public synthetic corpus with an explicitly pinned clean old checkout."""

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_RUNNER = r'''
import hashlib, json, os, platform, sys, time, unicodedata
from dataclasses import asdict
from pathlib import Path
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
import django
django.setup()
from apps.labs.dictionary import default_dictionary
from apps.labs.extraction import extract_observations
from apps.processing.value_objects import OcrPage, OcrRegion
dictionary = default_dictionary()
predictions = []
started = time.perf_counter()
for case in json.load(sys.stdin):
    pages = tuple(OcrPage(**{**page, 'regions': tuple(OcrRegion(**region) for region in page['regions'])}) for page in case['pages'])
    try:
        observations = [asdict(row) for row in extract_observations(pages, dictionary)]
        status = 'success' if observations else 'original_only'
    except Exception:
        observations, status = [], 'failed'
    predictions.append({'source_file_hash': case['source_file_hash'], 'status': status, 'observations': observations})
paths = sorted({*Path('apps').rglob('*.py'), *Path('config').rglob('*.py'), *Path('tools/sample_dictionary').rglob('*.py')})
identity = {'parser_files': {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    'dictionary_sha256': dictionary.content_hash, 'dictionary_version': dictionary.version,
    'runtime': {'python': platform.python_version(), 'unicode': unicodedata.unidata_version, 'django': django.get_version()}}
print(json.dumps({'predictions': predictions, 'identity': identity, 'execution': {'extraction_seconds': time.perf_counter()-started}}, ensure_ascii=False))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--revision', default='a8fe8bb0d0f87cfdd1f14320e780783da1ad02c7')
    parser.add_argument('--output', type=Path, default=ROOT/'apps/labs/dictionaries/phase-two-baseline.json')
    args = parser.parse_args(argv)
    checkout = args.checkout.resolve(strict=True)
    def git(*arguments):
        return subprocess.run(['git', *arguments], cwd=checkout, check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()
    revision = git('rev-parse', args.revision + '^{commit}')
    if git('rev-parse', 'HEAD') != revision or git('status', '--porcelain', '--', 'apps', 'config', 'tools'):
        raise ValueError('Baseline checkout must be clean and exactly at the requested revision')
    sys.path.insert(0, str(ROOT))
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
    import django
    django.setup()
    from apps.labs.dictionary import rules_digest
    from apps.labs.release_evaluation import frozen_release_cases, score_frozen_predictions
    from tools.phase_two_evaluation import digest
    cases = frozen_release_cases()
    inputs = [{'source_file_hash': case['source_file_hash'], 'pages': [asdict(page) for page in case['pages']]} for case in cases]
    completed = subprocess.run([sys.executable, '-c', HISTORICAL_RUNNER], cwd=checkout,
        input=json.dumps(inputs, ensure_ascii=False), capture_output=True, text=True, encoding='utf-8',
        env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}, timeout=180, check=True)
    historical = json.loads(completed.stdout)
    if git('rev-parse', 'HEAD') != revision or git('status', '--porcelain', '--', 'apps', 'config', 'tools'):
        raise ValueError('Baseline checkout changed during execution')
    evaluation = score_frozen_predictions(historical['predictions'])
    corpus_files = {key: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for key, path in (
        ('fixed_parser', 'apps/labs/dictionaries/phase-two-regression.json'),
        ('release_context', 'apps/labs/dictionaries/phase-two-release-evaluation.json'))}
    identity = {**historical['identity'], 'corpus_sha256': digest(corpus_files), 'corpus_files': corpus_files,
                'source_commit': revision, 'rules_sha256': rules_digest([]),
                'predictions_sha256': digest(historical['predictions'])}
    report = {'schema_version': 1, 'dataset_kind': 'SYNTHETIC', 'scope': 'frozen_historical_parser',
              'identity': identity, 'evaluation': evaluation, 'metrics_sha256': digest(evaluation),
              'execution': historical['execution'],
              'limits': ['Public synthetic OCR cells only; no patient sources.',
                         'Historical extraction is executed from the clean pinned checkout.',
                         'Only fields present in the old output are scored; absent fields remain missing.',
                         'Historical downstream trend eligibility was not measured; no claim about metadata extraction speed.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    print(json.dumps({'source_commit': revision, 'cases': len(cases), 'metrics_sha256': report['metrics_sha256']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
