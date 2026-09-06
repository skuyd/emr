"""Deterministic source-level laboratory evaluation; medical transcripts stay private."""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import time


FIELDS = ('standard_code', 'raw_name', 'specimen', 'raw_value', 'result_type', 'raw_unit',
          'reference_range_raw', 'report_date')
JOINT_FIELDS = ('standard_code', 'raw_value', 'result_type', 'raw_unit')
SEVERE_FIELDS = ('standard_code', 'raw_value', 'result_type', 'raw_unit')


class EvaluationError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _canonical(field, value):
    if value is None:
        return None
    raw = str(value)
    if field == 'raw_unit':
        raw = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+', lambda m: '^' + unicodedata.normalize('NFKC', m.group()), raw)
        return _text(raw).replace('µ', 'μ').replace('×', '*')
    text = _text(raw).replace('−', '-')
    if field == 'raw_name':
        return re.sub(r'^[\d\s★☆△*]+', '', text)
    if field == 'specimen':
        return {'全血': 'blood', '血清': 'blood', '血浆': 'blood', '血液': 'blood',
                '尿液': 'urine', '中段尿': 'urine', '尿': 'urine', '粪便': 'stool', '大便': 'stool'}.get(text, text)
    if field == 'raw_value':
        # Equivalent numeric spellings compare equally; inequalities remain inequalities.
        if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?', text):
            try:
                return str(Decimal(text).normalize())
            except InvalidOperation:
                pass
        return text.replace('≤', '<=').replace('≥', '>=')
    if field == 'reference_range_raw':
        return re.sub(r'-{2,}', '-', text.replace('—', '-').replace('～', '-').replace('~', '-'))
    return text


def _value(row, field):
    if field == 'report_date':
        return row.get('observation_date', row.get('report_date'))
    return row.get(field)


def _equal(field, expected, actual):
    left, right = _canonical(field, expected), _canonical(field, actual)
    return left is not None and left == right


def _name_agrees(first, second):
    a, b = _canonical('raw_name', first), _canonical('raw_name', second)
    return bool(a and b and (a == b or len(a) >= 2 and a in b or len(b) >= 2 and b in a))


def _score(expected, actual):
    if expected['page_number'] != actual['page_number']:
        return 0
    # Source labels determine correspondence before evaluating mapped codes/values.
    # Matching on the predicted code or result can hide a swapped mapping/value.
    exact = _equal('raw_name', expected.get('raw_name'), actual.get('raw_name'))
    name = 600 if exact else 300 if _name_agrees(expected.get('raw_name'), actual.get('raw_name')) else 0
    identity = 10 if not name and expected.get('standard_code') and expected['standard_code'] == actual.get('standard_code') else 0
    if not identity and not name:
        return 0
    order = max(0, 5 - abs(expected.get('row_index', 0) - actual.get('reading_order', 0)))
    return identity + name + order


def _assignment(gold, predicted):
    """Maximum-weight one-to-one matching (Hungarian), deterministic on duplicate rows."""
    if not gold or not predicted:
        return {}
    size = max(len(gold), len(predicted))
    if not size:
        return {}
    scores = [[_score(gold[i], predicted[j]) if i < len(gold) and j < len(predicted) else 0
               for j in range(size)] for i in range(size)]
    u, v, p, way = ([0] * (size + 1) for _ in range(4))
    for i in range(1, size + 1):
        p[0], j0 = i, 0
        minimum, used = [float('inf')] * (size + 1), [False] * (size + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float('inf'), 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                cost = -scores[i0 - 1][j - 1] - u[i0] - v[j]
                if cost < minimum[j]:
                    minimum[j], way[j] = cost, j0
                if minimum[j] < delta:
                    delta, j1 = minimum[j], j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            p[j0] = p[way[j0]]
            j0 = way[j0]
    return {p[j] - 1: j - 1 for j in range(1, size + 1)
            if 0 < p[j] <= len(gold) and j <= len(predicted) and scores[p[j] - 1][j - 1] > 0}


def _rates(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {'true_positive': tp, 'false_positive': fp, 'false_negative': fn,
            'precision': precision, 'recall': recall,
            'f1': 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}


def _intercepted(row):
    # This is a source-transcription interception count, not a clinical severity assessment.
    codes = {item.get('code') for item in row.get('quality_issues', [])}
    return bool(row.get('capability_level') != 'STABLE' or row.get('confidence', 0) < .95
                or codes - {'reference_unknown', 'reference_conflict'})


def _localization(expected, actual):
    eligible = correct = 0
    for field, source in expected.get('field_sources', {}).items():
        box = source.get('region')
        if not box or source.get('granularity') == 'page':
            continue
        eligible += 1
        observed = actual.get('field_evidence', {}).get('observation_date' if field == 'report_date' else field, {}) if actual else {}
        polygon = observed.get('polygon')
        if observed.get('precision') != 'region' or not polygon or observed.get('page_number') != source['page_number']:
            continue
        xs, ys = zip(*polygon)
        candidate = (min(xs), min(ys), max(xs), max(ys))
        overlap = max(0, min(box[2], candidate[2]) - max(box[0], candidate[0])) * max(0, min(box[3], candidate[3]) - max(box[1], candidate[1]))
        union = (box[2] - box[0]) * (box[3] - box[1]) + (candidate[2] - candidate[0]) * (candidate[3] - candidate[1]) - overlap
        correct += int(bool(union > 0 and overlap / union >= .5))
    return eligible, correct


def _assessment_scopes(annotations, classification):
    reports = {item['report_group_id']: item for item in (*classification.get('reports', []), *annotations.get('reports', []))}
    pages = defaultdict(lambda: defaultdict(set))
    for row in annotations['rows']:
        if row.get('source_verified'):
            pages[row['source_file_hash']][row['page_number']].add(row['report_group_id'])
    scopes = {}
    for item in classification['files']:
        kind = item.get('document_type', '').lower()
        non_lab = (kind.startswith(('imaging', 'pathology', 'molecular')) or kind in {
            'medication_orders', 'discharge_record', 'admission_record', 'progress_notes', 'treatment', 'order', 'prescription',
        })
        file_groups = set(item.get('report_group_ids', []))
        complete = {page for page, groups in pages[item['source_file_hash']].items()
                    if groups and all(reports.get(group, {}).get('target_rows_complete') is True
                                      for group in groups | file_groups)}
        scopes[item['source_file_hash']] = {'all': non_lab, 'complete_pages': complete}
    return scopes


def evaluate_predictions(predictions, annotations, classification, *, _include_subset=True, _assessment_scope=None):
    files = classification['files']
    source_ids = {item['source_file_hash'] for item in files}
    if len(source_ids) != len(files):
        raise EvaluationError('Duplicate classified source')
    indexed = {item['source_file_hash']: item for item in predictions}
    if len(indexed) != len(predictions) or indexed.keys() - source_ids:
        raise EvaluationError('Duplicate or foreign prediction source')
    if any(row['source_file_hash'] not in source_ids for row in annotations['rows']):
        raise EvaluationError('Annotation source is outside frozen inventory')
    statuses = Counter(item.get('status', 'failed') for item in predictions)
    gold_by_source = defaultdict(list)
    for row in annotations['rows']:
        if row.get('source_verified'):
            gold_by_source[row['source_file_hash']].append(row)
    fields = {name: [0, 0, 0] for name in FIELDS}
    joint = [0, 0, 0]
    severe = {'errors': 0, 'intercepted': 0, 'unintercepted': 0, 'assessed_predictions': 0, 'unassessed_predictions': 0,
              'joint_correct_assessed': 0, 'joint_correct_intercepted': 0}
    localization = {'eligible_fields': 0, 'correct_fields': 0}
    trend = {'assessed': 0, 'eligible': 0}
    groups = set()
    errors = []
    eligible_prediction_count = 0
    unassessed_predictions = 0
    scopes = _assessment_scope if _assessment_scope is not None else _assessment_scopes(annotations, classification)
    for item in files:
        source = item['source_file_hash']
        gold = gold_by_source[source]
        prediction = indexed.get(source, {})
        predicted = [{**row, 'observation_date': row.get('observation_date', row.get('report_date',
                         prediction.get('metadata', {}).get('document_date')))}
                     for row in prediction.get('observations', []) if not (row.get('standard_code') or '').startswith('GENE_')]
        scope = scopes[source]
        matched = _assignment(gold, predicted)
        used = set(matched.values())
        eligible_prediction_count += len(matched)
        for index, expected in enumerate(gold):
            groups.add(expected['report_group_id'])
            actual = predicted[matched[index]] if index in matched else None
            eligible, correct = _localization(expected, actual)
            localization['eligible_fields'] += eligible
            localization['correct_fields'] += correct
            for field in FIELDS:
                if expected.get(field) is None:
                    continue
                if actual is not None and _equal(field, expected[field], _value(actual, field)):
                    fields[field][0] += 1
                else:
                    fields[field][2] += 1
                    if actual is not None and _value(actual, field) is not None:
                        fields[field][1] += 1
            joint_eligible = all(expected.get(field) is not None for field in JOINT_FIELDS)
            if joint_eligible:
                if actual is not None and all(_equal(field, expected[field], _value(actual, field)) for field in JOINT_FIELDS):
                    joint[0] += 1
                    severe['joint_correct_assessed'] += 1
                    severe['joint_correct_intercepted'] += _intercepted(actual)
                else:
                    joint[2] += 1
                    if actual is not None:
                        joint[1] += 1
            if actual is not None:
                if any(expected.get(field) is not None for field in SEVERE_FIELDS):
                    severe['assessed_predictions'] += 1
                else:
                    severe['unassessed_predictions'] += 1
                wrong = [field for field in SEVERE_FIELDS if expected.get(field) is not None and not _equal(field, expected[field], _value(actual, field))]
                if wrong:
                    severe['errors'] += 1
                    intercepted = _intercepted(actual)
                    severe['intercepted' if intercepted else 'unintercepted'] += 1
                    errors.append({'source_file_hash': source, 'annotation_id': expected['annotation_id'],
                                   'prediction_order': actual.get('reading_order'), 'fields': wrong, 'intercepted': intercepted})
        for index, actual in enumerate(predicted):
            if actual.get('trend_eligible') is not None:
                trend['assessed'] += 1
                trend['eligible'] += bool(actual['trend_eligible'])
            if index in used:
                continue
            if not scope['all'] and actual['page_number'] not in scope['complete_pages']:
                unassessed_predictions += 1
                severe['unassessed_predictions'] += 1
                continue
            eligible_prediction_count += 1
            joint[1] += 1
            for field in FIELDS:
                if _value(actual, field) is not None:
                    fields[field][1] += 1
            severe['assessed_predictions'] += 1
            severe['errors'] += 1
            intercepted = _intercepted(actual)
            severe['intercepted' if intercepted else 'unintercepted'] += 1
            errors.append({'source_file_hash': source, 'annotation_id': None, 'prediction_order': actual.get('reading_order'),
                           'fields': ['unmatched_project'], 'intercepted': intercepted})
    report = {
        'schema_version': 1, 'annotation_review_status': annotations.get('review_status', 'primary_only'),
        'files': {'total': len(files), 'success': statuses['success'], 'original_only': statuses['original_only'],
                  'failed': sum(value for key, value in statuses.items() if key not in {'success', 'original_only'}),
                  'missing': len(source_ids - indexed.keys())},
        'annotated_rows': sum(len(rows) for rows in gold_by_source.values()), 'annotated_report_groups': len(groups),
        'annotated_source_files': sum(bool(rows) for rows in gold_by_source.values()),
        'assessed_predictions': eligible_prediction_count, 'unassessed_prediction_rows': unassessed_predictions,
        'joint': _rates(*joint),
        'fields': {key: _rates(*value) for key, value in fields.items()}, 'severe': severe,
        'localization': {**localization, 'exact_rate': localization['correct_fields'] / localization['eligible_fields'] if localization['eligible_fields'] else None},
        'trend': {**trend, 'eligible_ratio': trend['eligible'] / trend['assessed'] if trend['assessed'] else None},
        'errors': errors,
        'limits': ['Development regression; no generalization claim.',
                   'Rows count source representations, including independently retained duplicate photos/pages.',
                   'Unknown fields and unmatched rows outside explicitly complete annotation scopes are unassessed; failed files remain in full counts.',
                   'Severe count conservatively includes every verified value, unit, type or project error, including intercepted ones.',
                   'Exact localization requires independently annotated field boxes and intersection-over-union >=0.5; page fallback never qualifies.'],
    }
    severe['error_rate'] = severe['errors'] / severe['assessed_predictions'] if severe['assessed_predictions'] else None
    severe['interception_recall'] = severe['intercepted'] / severe['errors'] if severe['errors'] else None
    severe['correct_transcription_routed_ratio'] = (severe['joint_correct_intercepted'] / severe['joint_correct_assessed']
        if severe['joint_correct_assessed'] else None)
    if _include_subset:
        success = {item['source_file_hash'] for item in predictions if item['status'] == 'success'}
        sub = evaluate_predictions([item for item in predictions if item['source_file_hash'] in success],
            {**annotations, 'rows': [row for row in annotations['rows'] if row['source_file_hash'] in success]},
            {**classification, 'files': [item for item in files if item['source_file_hash'] in success]}, _include_subset=False, _assessment_scope=scopes)
        report['successful_subset'] = {key: sub[key] for key in ('annotated_rows', 'annotated_report_groups', 'joint', 'fields', 'severe')}
    report['metrics_sha256'] = digest(report)
    return report


def regression_gate(current, baseline):
    failures = []
    if current.get('annotation_review_status') != 'adjudicated':
        failures.append('independent_annotations_not_adjudicated')
    if not current['annotated_rows']:
        failures.append('no_annotated_rows')
    if current['files']['total'] != baseline['files']['total'] or current['annotated_rows'] != baseline['annotated_rows']:
        failures.append('baseline_denominator_changed')
    for field in FIELDS:
        previous, now = baseline['fields'][field]['recall'], current['fields'][field]['recall']
        if previous is not None and (now is None or now < previous):
            failures.append(f'recall_regressed:{field}')
    if current['severe']['unintercepted']:
        failures.append('known_severe_errors_not_intercepted')
    return {'passed': not failures, 'failures': failures,
            'baseline_metrics_sha256': baseline['metrics_sha256'], 'candidate_metrics_sha256': current['metrics_sha256']}


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def capture_parser_identity(root):
    """Bind interpretation to application dependencies, tier definitions and runtime."""
    import platform
    import sqlite3
    import django
    from decimal import getcontext
    names = {path.relative_to(root).as_posix() for directory in ('apps', 'config', 'tools/sample_dictionary')
             for path in (root/directory).rglob('*.py')}
    names.add('tools/phase_two_evaluation.py')
    files = {name: file_digest(root/name) for name in sorted(names)}
    context = getcontext()
    runtime = {'python': platform.python_version(), 'unicode': unicodedata.unidata_version,
        'django': django.get_version(), 'sqlite': sqlite3.sqlite_version,
        'decimal': {key: getattr(context, key) for key in ('prec', 'Emin', 'Emax', 'rounding')}}
    identity = {'files': files, 'runtime': runtime, 'tier_coverage_sha256': file_digest(root/'apps/labs/dictionaries/phase-two-coverage.json')}
    return {**identity, 'sha256': digest(identity)}


def load_frozen_pages(source, cache_directory, manifest_sample):
    """Reject stale originals/caches before replaying a frozen OCR provider result."""
    from apps.processing.value_objects import OcrPage, OcrRegion
    identity = source['hash']
    if file_digest(source['path']) != identity or manifest_sample['source_file_hash'] != identity:
        raise EvaluationError('Frozen source identity changed')
    cache = Path(cache_directory) / f'{identity}.json'
    if file_digest(cache) != manifest_sample['ocr_cache_sha256']:
        raise EvaluationError('Frozen OCR cache identity changed')
    content = json.loads(cache.read_text(encoding='utf-8'))
    if content.get('source_file_hash') != identity or len(content['pages']) != manifest_sample['ocr_pages']:
        raise EvaluationError('Frozen OCR cache source/page identity changed')
    pages = tuple(OcrPage(**{**page, 'provider_metadata': tuple(page['provider_metadata'].items()),
        'regions': tuple(OcrRegion(**region) for region in page['regions'])}) for page in content['pages'])
    if tuple(page.page_number for page in pages) != tuple(range(1, len(pages) + 1)):
        raise EvaluationError('Frozen OCR cache page sequence changed')
    return pages


def predict_frozen_sources(sources, cache_directory, baseline_manifest, classification, dictionary, *, progress=None):
    """Replay extraction, actual transactional persistence and effective comparison in a test database.

    The CLI creates an ephemeral in-memory database. This callable also supports pytest's isolated DB.
    No OCR or image-preparation timing is claimed: inputs are frozen provider results.
    """
    from dataclasses import asdict
    from django.contrib.auth import get_user_model
    from django.db import connection
    from apps.documents.models import Document, DocumentPage, ProcessingRun, ProcessingStage, UploadBatch
    from apps.labs.comparison import comparable_cell
    from apps.labs.extraction import extract_observations
    from apps.labs.readmodels import effective_rows
    from apps.patients.models import Patient
    from apps.processing.metadata import extract_document_metadata
    from apps.processing.models import ParsingVersion
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, PipelineResult, run_processing

    if connection.vendor != 'sqlite' or not ('memory' in str(connection.settings_dict['NAME'])):
        raise EvaluationError('Evaluation persistence requires an ephemeral in-memory SQLite database')
    manifest = {item['source_file_hash']: item for item in baseline_manifest['samples']}
    classified = {item['source_file_hash']: item for item in classification['files']}
    if len(manifest) != len(sources) or set(manifest) != {item['hash'] for item in sources} or set(manifest) != set(classified):
        raise EvaluationError('Frozen source/classification/baseline set differs')
    patients, predictions, identities, timings = {}, [], {}, []
    started = time.perf_counter()

    class CachedPipeline(DocumentProcessingPipeline):
        def run(self, context):
            document = Document.objects.get(pk=context.document_id)
            context.heartbeat(ProcessingStage.CLASSIFYING)
            self.extracted = extract_observations(self.pages, self.dictionary)
            self.metadata = extract_document_metadata(self.pages, observation_count=len(self.extracted))
            context.heartbeat(ProcessingStage.EXTRACTING)
            self._persist(context, document, self.pages, self.extracted, self.metadata, ())
            context.heartbeat(ProcessingStage.INDEXING)
            return PipelineResult.organized() if any(page.regions for page in self.pages) else PipelineResult.original_only()

    for source in sources:
        start = time.perf_counter()
        identity = source['hash']
        pages = load_frozen_pages(source, cache_directory, manifest[identity])
        group = classified[identity].get('patient_group_id') or identity
        if group not in patients:
            account = get_user_model().objects.create(phone_hash=hashlib.sha256(('evaluation:' + group).encode()).hexdigest(),
                                                      phone_encrypted='isolated-evaluation-placeholder')
            patients[group] = Patient.objects.create(account=account, display_name='隔离评测')
        patient = patients[group]
        batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=len(pages), byte_size=Path(source['path']).stat().st_size)
        document = Document.objects.create(patient=patient, batch=batch, display_filename=f'evaluation-{identity[:12]}.bin',
            content_type='application/pdf', byte_size=Path(source['path']).stat().st_size, page_count=len(pages),
            sha256=identity, original_object_key=f'originals/evaluation/{identity}')
        DocumentPage.objects.bulk_create([DocumentPage(document=document, page_number=page.page_number, width=page.width, height=page.height) for page in pages])
        run = ProcessingRun.objects.create(document=document, parser_version='phr-v2', task_type='fixed_evaluation',
            idempotency_key=f'{document.pk}:phr-v2:fixed_evaluation')
        pipeline = CachedPipeline(object_store=None, raster_provider=None, dictionary=dictionary)
        pipeline.pages = pages
        result = run_processing(run.pk, pipeline)
        if result.state in {ExecutionState.SUCCEEDED, ExecutionState.NO_STRUCTURED_RESULT}:
            version = ParsingVersion.objects.get(processing_run=run)
            rows = [asdict(row) for row in pipeline.extracted]
            predictions.append({'source_file_hash': identity, 'status': 'success' if rows else 'original_only',
                'observations': rows, 'metadata': asdict(pipeline.metadata)})
            identities[version.pk] = predictions[-1]
        else:
            predictions.append({'source_file_hash': identity, 'status': 'failed', 'observations': [], 'failure_state': result.state.value})
        timings.append({'source_file_hash': identity, 'seconds': time.perf_counter() - start})
        if progress:
            progress(len(predictions), len(sources), predictions[-1]['status'])
    comparison_started = time.perf_counter()
    for patient in patients.values():
        rows = effective_rows(patient, include_uncertain=True)
        for row in rows:
            prediction = identities[row.parsing_version_id]
            item = next(item for item in prediction['observations'] if item['page_number'] == row.document_page.page_number and item['reading_order'] == row.reading_order)
            cell = comparable_cell(row, previous=rows)
            item.update(observation_date=row.observation_date.isoformat() if row.observation_date else None,
                field_evidence=row.field_evidence, quality_issues=list(cell.quality_issues),
                trend_eligible=cell.trend_eligible, comparability=cell.comparability)
    # Round-trip serializes dates and immutable enum values while excluding nondeterministic DB IDs.
    predictions = json.loads(json.dumps(predictions, ensure_ascii=False, default=str))
    return {'predictions': predictions, 'execution': {'persistence_and_comparison': True, 'database': connection.vendor,
        'ocr_replayed': True, 'ocr_and_preparation_measured': False, 'files': timings,
        'comparison_seconds': time.perf_counter() - comparison_started, 'total_seconds': time.perf_counter() - started}}


def quality_breakdowns(predictions, annotations, classification, coverage):
    targets = {item['code']: item for item in coverage['target_definitions']}
    gold = defaultdict(list)
    for row in annotations['rows']:
        if row.get('source_verified'):
            gold[row['source_file_hash']].append(row)
    by_tier = {tier: [] for tier in ('TIER_1', 'TIER_2')}
    unassigned = 0
    matched_rows = {}
    extra_rows = defaultdict(list)
    for prediction in predictions:
        source = prediction['source_file_hash']
        rows = [row for row in prediction['observations'] if not (row.get('standard_code') or '').startswith('GENE_')]
        match = _assignment(gold[source], rows)
        reverse = {j: i for i, j in match.items()}
        for i, j in match.items():
            matched_rows[gold[source][i]['annotation_id']] = rows[j]
        split = defaultdict(list)
        for j, row in enumerate(rows):
            expected = gold[source][reverse[j]] if j in reverse else {}
            code = expected.get('standard_code') if j in reverse else row.get('standard_code')
            tier = targets.get(code, {}).get('tier')
            if tier in by_tier:
                split[tier].append(row)
            elif not (row.get('standard_code') or '').startswith('GENE_'):
                unassigned += 1
            if j not in reverse:
                extra_rows[(source, row['page_number'])].append(row)
        for tier in by_tier:
            by_tier[tier].append({**prediction, 'observations': split[tier]})
    tiers = {}
    for tier, records in by_tier.items():
        subset = {**annotations, 'rows': [row for row in annotations['rows'] if targets.get(row.get('standard_code'), {}).get('tier') == tier]}
        result = evaluate_predictions(records, subset, classification, _include_subset=False,
                                      _assessment_scope=_assessment_scopes(annotations, classification))
        tiers[tier] = {key: result[key] for key in ('annotated_rows', 'annotated_report_groups', 'joint', 'fields', 'severe')}
    represented = Counter(row.get('standard_code') for row in annotations['rows'] if row.get('source_verified'))
    dual_groups = {report['report_group_id'] for report in classification.get('reports', [])
                   if report.get('layout') == 'dual_column' and report.get('target_rows_complete')}
    group_rows = defaultdict(list)
    for row in annotations['rows']:
        if row['report_group_id'] in dual_groups and row.get('standard_code') in targets and row.get('source_verified'):
            group_rows[row['report_group_id']].append(row)
    def complete_group(rows):
        if not all(row['annotation_id'] in matched_rows and all(_equal(field, row[field], matched_rows[row['annotation_id']].get(field))
                   for field in ('standard_code', 'raw_value')) for row in rows):
            return False
        for row in rows:
            extras = extra_rows[(row['source_file_hash'], row['page_number'])]
            if any(extra.get('standard_code') in targets or _name_agrees(row.get('raw_name'), extra.get('raw_name')) for extra in extras):
                return False
        return True
    correct = sum(complete_group(rows) for rows in group_rows.values())
    return {'tiers': tiers, 'unassigned_prediction_rows': unassigned,
        'target_coverage': {code: {'source_rows': represented[code], 'real_status': 'assessed' if represented[code] else 'not_evaluated'} for code in targets},
        'dual_column': {'assessed_report_groups': len(group_rows), 'complete_target_association_groups': correct,
            'rate': correct / len(group_rows) if group_rows else None,
            'scope': 'Strict whole-report code/value match across all available source representations; missing rows or OCR value differences also fail. This does not isolate geometric from OCR errors.'},
        'patients': len({item['patient_group_id'] for item in classification['files'] if item.get('patient_group_id')}),
        'institutions': len({item['institution_group_id'] for item in classification['files'] if item.get('institution_group_id')}),
        'layouts': dict(Counter(str(item.get('layout', 'unknown')) for item in classification['files']))}


def register_evaluation_snapshot(dictionary, rules):
    """Make an unpublished snapshot resolvable only in the isolated replay database."""
    from django.db import connection
    from django.utils import timezone
    from apps.labs.dictionary import load_dictionary_content, release_digest, rules_digest
    from apps.operations.models import DictionaryRelease
    if connection.vendor != 'sqlite' or str(connection.settings_dict['NAME']) not in {':memory:', 'file:memorydb_default?mode=memory&cache=shared'}:
        raise EvaluationError('Snapshot registration requires an isolated in-memory evaluation database')
    payload = json.loads(dictionary.source_path.read_text(encoding='utf-8'))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    snapshot = load_dictionary_content(encoded)
    DictionaryRelease.objects.create(version=snapshot.version, content_hash=snapshot.content_hash, payload=payload,
        artifact_name='', indicator_count=len(snapshot.indicators), rules=list(rules), rules_hash=rules_digest(rules),
        release_hash=release_digest(snapshot.content_hash, rules), published_at=timezone.now(), active=True)
    return snapshot


def main(argv=None):
    import argparse
    import os
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description='Frozen development regression; private source transcripts never enter the public report.')
    parser.add_argument('--synthetic-only', action='store_true')
    parser.add_argument('--source-map', type=Path)
    parser.add_argument('--ocr-cache-dir', type=Path)
    parser.add_argument('--annotations', type=Path)
    parser.add_argument('--classification', type=Path)
    parser.add_argument('--baseline-predictions', type=Path)
    parser.add_argument('--dictionary', type=Path, default=root/'apps/labs/dictionaries/phase-two.json')
    parser.add_argument('--rules', type=Path, help='Reviewed rules JSON array for this exact dictionary snapshot')
    parser.add_argument('--baseline-report', type=Path, default=root/'apps/labs/dictionaries/phase-two-baseline.json')
    parser.add_argument('--baseline-manifest', type=Path, default=root/'docs/verification/artifacts/phase-two-baseline-manifest.json')
    parser.add_argument('--annotation-manifest', type=Path, default=root/'docs/verification/artifacts/phase-two-annotation-adjudication-report.json')
    parser.add_argument('--private-output', type=Path, default=root/'.runtime/phase-two-evaluation')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
    import django
    django.setup()
    from apps.labs.dictionary import load_dictionary, rules_digest
    dictionary = load_dictionary(args.dictionary)
    read = lambda path: json.loads(path.read_text(encoding='utf-8'))
    rules = read(args.rules) if args.rules else []
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise EvaluationError('Rules snapshot must be a JSON array of rule records')
    args.report = args.report or root/'docs/verification/artifacts'/('phase-two-release-evaluation.json' if args.synthetic_only else 'phase-two-real-evaluation.json')
    if args.synthetic_only:
        from apps.labs.release_evaluation import evaluate_release_snapshot
        report = evaluate_release_snapshot(dictionary, rules, baseline_report=read(args.baseline_report))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'metrics_sha256': report['metrics_sha256'], 'report': str(args.report)}, ensure_ascii=False))
        return 0 if report['passed'] else 2
    for field in ('source_map', 'ocr_cache_dir', 'annotations', 'classification', 'baseline_predictions'):
        if getattr(args, field) is None:
            parser.error('--' + field.replace('_', '-') + ' is required for real-source evaluation')
    adjudication = read(args.annotation_manifest)
    if adjudication.get('status') != 'adjudicated' or file_digest(args.annotations) != adjudication['annotations_sha256'] or file_digest(args.classification) != adjudication['classification_sha256']:
        raise EvaluationError('Frozen adjudicated annotation/classification identity changed')
    annotations, classification, manifest = read(args.annotations), read(args.classification), read(args.baseline_manifest)
    baseline_predictions = read(args.baseline_predictions)
    frozen = {item['source_file_hash']: item for item in manifest['samples']}
    if len(baseline_predictions) != len(frozen) or {item['source_file_hash'] for item in baseline_predictions} != set(frozen):
        raise EvaluationError('Frozen baseline source set changed')
    for prediction in baseline_predictions:
        expected = frozen[prediction['source_file_hash']]['prediction_sha256']
        if hashlib.sha256(json.dumps(prediction, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest() != expected:
            raise EvaluationError('Frozen baseline prediction changed')
    from django.core.management import call_command
    from django.db import connection
    if connection.vendor != 'sqlite' or str(connection.settings_dict['NAME']) != ':memory:':
        raise EvaluationError('CLI evaluation requires a new in-memory SQLite database')
    call_command('migrate', verbosity=0)
    input_dictionary_hash = dictionary.content_hash
    if rules or args.dictionary.resolve() != (root/'apps/labs/dictionaries/phase-two.json').resolve():
        dictionary = register_evaluation_snapshot(dictionary, rules)
    parser_identity = capture_parser_identity(root)
    output = predict_frozen_sources(read(args.source_map), args.ocr_cache_dir, manifest, classification, dictionary,
        progress=lambda done, total, status: print(f'{done}/{total} {status}', flush=True))
    baseline = evaluate_predictions(baseline_predictions, annotations, classification)
    current = evaluate_predictions(output['predictions'], annotations, classification)
    gate = regression_gate(current, baseline)
    coverage = read(root/'apps/labs/dictionaries/phase-two-coverage.json')
    if capture_parser_identity(root) != parser_identity:
        raise EvaluationError('Parser dependencies changed during evaluation; repeat with a stable checkout')
    report = {'schema_version': 1, 'scope': 'real_source_development_regression', 'gate': gate,
        'baseline': baseline, 'current': current, 'breakdowns': quality_breakdowns(output['predictions'], annotations, classification, coverage),
        'execution': output['execution'], 'identity': {'source_set': digest(sorted(frozen)),
            'annotations_sha256': file_digest(args.annotations), 'classification_sha256': file_digest(args.classification),
            'baseline_manifest_sha256': file_digest(args.baseline_manifest), 'baseline_predictions_sha256': file_digest(args.baseline_predictions),
            'dictionary_version': dictionary.version, 'dictionary_hash': dictionary.content_hash,
            'dictionary_input_sha256': input_dictionary_hash, 'rules_hash': rules_digest(rules),
            'parser_files': parser_identity['files'], 'runtime': parser_identity['runtime'],
            'tier_coverage_sha256': parser_identity['tier_coverage_sha256'],
            'parser_identity_sha256': parser_identity['sha256'], 'predictions_sha256': digest(output['predictions'])},
        'limits': ['All 64 original file hashes and frozen OCR bytes verified; no fresh OCR/preparation speed claim.',
            'Actual pipeline persistence, effective-result validation and comparison executed in an isolated in-memory test database.',
            'The explicit reviewed rule snapshot is used for downstream validation/comparison; an omitted --rules means no enabled rules.',
            'Correctly transcribed rows may still be routed for review due to missing date, method, specimen or source confidence; this ratio is not a clinical false-positive rate.',
            'Patient transcripts and source paths are excluded from this public artifact; predictions/annotations remain private.',
            'Real and synthetic evaluations are separate; development samples do not establish external accuracy.']}
    args.private_output.mkdir(parents=True, exist_ok=True)
    (args.private_output/'current-predictions.json').write_text(json.dumps(output['predictions'], ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'gate': gate, 'files': current['files'], 'joint': current['joint'], 'severe': current['severe'], 'trend': current['trend']}, ensure_ascii=False))
    return 0 if gate['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
