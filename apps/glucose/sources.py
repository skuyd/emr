"""Current blood-glucose sources and per-field provenance; no imported confirmation is implicit."""

from copy import deepcopy
import unicodedata

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.documents.locking import lock_document_aggregate
from apps.facts.readmodels import digest
from apps.labs.models import LabObservation
from apps.labs.revisions import VALUE_FIELDS, effective_observation
from apps.patients.access import Capability, authorize_patient
from apps.processing.material_review import material_state

from .models import GlucoseRecord
from .payloads import normalize_payload
from .source_context import report_context


class GlucoseSourceUnavailable(ValueError):
    pass


def _raw_name(value):
    return ''.join(unicodedata.normalize('NFKC', value).split()).strip('*#★').casefold()


def _document_identity(document, version):
    if document.deleted_at is not None or document.purged_at is not None:
        raise GlucoseSourceUnavailable('原件已移出正常资料，请恢复后重新核对。')
    state = material_state(document, version)
    if state['status'] == 'NON_DOCUMENT':
        raise GlucoseSourceUnavailable('原件尚未按资料保留，请先核对资料类型。')
    return {'document_id': str(document.pk), 'source_sha256': document.sha256,
            'lifecycle_revision': document.lifecycle_revision, 'material_revision': document.material_revision,
            'material_override': document.material_override, 'material': state,
            'active_parsing_version_id': str(version.pk) if version else None,
            'published_at': version.published_at.isoformat() if version and version.published_at else None}


def lock_lab(patient, observation_id):
    """Caller holds the patient guard; lock the immutable parent before reading child evidence."""
    try:
        document_id = LabObservation.objects.filter(pk=observation_id, parsing_version__document__patient=patient).values_list(
            'parsing_version__document_id', flat=True).first()
    except (ValidationError, ValueError, TypeError):
        raise PermissionDenied from None
    if document_id is None:
        raise PermissionDenied
    document, _ = lock_document_aggregate(document_id, patient_id=patient.pk)
    if document is None:
        raise PermissionDenied
    observation = LabObservation.objects.select_related('parsing_version__document', 'document_page', 'evidence').filter(
        pk=observation_id, parsing_version__document=document).first()
    if observation is None:
        raise GlucoseSourceUnavailable('来源观察项已不可用，请打开当前报告。')
    observation.parsing_version.document = document
    return observation


def _field_sources(observation, effective):
    source_ids = {source.get('observation_id') for source in effective.value_sources.values()}
    originals = {str(item.pk): item for item in LabObservation.objects.filter(
        pk__in=source_ids, parsing_version__document_id=observation.parsing_version.document_id,
    ).select_related('parsing_version', 'document_page', 'evidence')}
    result = {}
    for field in VALUE_FIELDS:
        identity = effective.value_sources.get(field, {})
        original = originals.get(identity.get('observation_id'))
        if original is None or str(original.evidence_id) != identity.get('evidence_id'):
            raise GlucoseSourceUnavailable('保留字段的原始证据已不可用，请先处理报告核对。')
        revision_id = identity.get('revision_id')
        if revision_id and not original.revisions.filter(pk=revision_id).exists():
            raise GlucoseSourceUnavailable('保留字段的修订来源已不可用。')
        value = getattr(effective, field)
        if hasattr(value, 'isoformat'):
            value = value.isoformat()
        result[field] = {**identity, 'document_id': str(original.parsing_version.document_id),
            'parsing_version_id': str(original.parsing_version_id), 'page_id': str(original.document_page_id),
            'page_number': original.document_page.page_number, 'observation_revision': original.revision_number,
            'field_evidence': deepcopy(original.field_evidence.get(field, {})),
            'original_text': original.evidence.source_text, 'original_polygon': deepcopy(original.evidence.polygon),
            'effective_value': value}
    return result, originals


def lab_candidate(observation):
    """Build from persisted current evidence. Call under the source document lock for writes/exports."""
    version, document = observation.parsing_version, observation.parsing_version.document
    if not version.active or version.status != 'PUBLISHED' or version.published_at is None:
        raise GlucoseSourceUnavailable('解析版本已变化，请打开当前报告后重新核对。')
    doc_identity = _document_identity(document, version)
    effective = effective_observation(observation)
    if effective.reported_error or effective.revision_conflict:
        raise GlucoseSourceUnavailable('报告字段存在识别反馈或重解析冲突，请先在来源报告中核对。')
    names = {'glu', '葡萄糖', '血糖', '空腹血糖', '空腹葡萄糖', '血葡萄糖', '葡萄糖(glu)', '血糖(glu)'}
    if (effective.standard_code != 'LAB_FASTING_GLUCOSE' or _raw_name(effective.raw_name) not in names
            or effective.specimen != 'BLOOD'):
        raise GlucoseSourceUnavailable('只有明确的血液葡萄糖测量可作为血糖记录来源。')
    field_sources, originals = _field_sources(observation, effective)
    specimen_source = originals[field_sources['specimen']['observation_id']]
    if specimen_source.specimen != 'BLOOD':
        raise GlucoseSourceUnavailable('原始标本不是明确血液，不能根据字典映射替换标本。')
    value_source = originals[field_sources['raw_value']['observation_id']]
    blocks = list(value_source.parsing_version.ocr_blocks.filter(document_page_id=value_source.document_page_id).order_by('reading_order').values(
        'id', 'text', 'polygon', 'layout_polygon', 'reading_order'))
    for item in blocks:
        item['id'] = str(item['id'])
    context = report_context(blocks, anchor_polygon=field_sources['raw_value']['field_evidence'].get('polygon')
                             or value_source.evidence.polygon)
    if context['specimen_raw'] in ('中段尿', '尿液', '尿'):
        raise GlucoseSourceUnavailable('原件面板注明尿液标本，不能导入血糖曲线。')
    sample = context['sample_time']
    local, precision = sample['local'], sample['precision']
    if effective.date_verified and effective.observation_date is not None:
        corrected_date = effective.observation_date.isoformat()
        if not local or local[:10] != corrected_date:
            local, precision = corrected_date, 'DAY'
    # A date-only lab metadata value may be a report date. Without a sampling
    # label or a user-verified measurement date it cannot become a measurement.
    slot = context['time_slot']
    slot_origin = 'SOURCE_OCR' if slot == 'FASTING' else 'NOT_STATED'
    if _raw_name(effective.raw_name) in {'空腹血糖', '空腹葡萄糖'}:
        slot = 'FASTING'
        slot_origin = 'LAB_REVISION' if field_sources['raw_name']['revision_id'] else 'SOURCE_OCR'
    data = normalize_payload({'value': effective.raw_value, 'unit': effective.raw_unit,
        'measured_local': local, 'time_precision': precision, 'timezone': sample['timezone'],
        'timezone_origin': sample['timezone_origin'], 'time_slot': slot,
        'source_label': context['specimen_raw'] or '血液检验（具体标本未确认）', 'notes': ''},
        source_kind='LAB_REPORT', allow_imprecise=True)
    source = {**doc_identity, 'observation_id': str(observation.pk), 'page_id': str(observation.document_page_id),
              'page_number': observation.document_page.page_number, 'parsing_version_id': str(version.pk),
              'parser_version': version.parser_version, 'dictionary_version': effective.mapping_dictionary_version,
              'observation_revision': observation.revision_number,
              'applied_revision_id': str(effective.applied_revision.pk) if effective.applied_revision else None,
              'field_sources': field_sources, 'report_context': context, 'ocr_blocks_sha256': digest(blocks),
              'quality_issues': deepcopy(effective.quality_issues), 'value_origin': effective.value_origin}
    data['source'] = source
    data['field_origins'] = {'value': 'LAB_REVISION' if field_sources['raw_value']['revision_id'] else 'SOURCE_OCR',
        'unit': 'LAB_REVISION' if field_sources['raw_unit']['revision_id'] else 'SOURCE_OCR',
        'time': 'LAB_REVISION' if effective.date_verified and precision == 'DAY' else 'SOURCE_OCR',
        'timezone': sample['timezone_origin'], 'time_slot': slot_origin,
        'source_label': 'SOURCE_OCR', 'notes': 'USER_ENTERED'}
    return {'data': data, 'source_fingerprint': digest(source)}


def preview_lab(patient, actor, observation_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.READ, lock=True)
        observation = lock_lab(access.patient, observation_id)
        result = lab_candidate(observation)
        existing = GlucoseRecord.objects.filter(patient=access.patient, source_observation=observation).first()
        return {**result, 'existing_record_id': str(existing.pk) if existing else None,
                'existing_revision': existing.revision_number if existing else None}


def source_current(record):
    if record.source_kind in ('MANUAL', 'METER'):
        return True
    if record.source_kind == 'NURSING':
        from apps.documents.models import DocumentPage

        page = DocumentPage.objects.select_related('document').filter(pk=record.source_page_id,
            document_id=record.source_document_id, document__patient_id=record.patient_id).first()
        if page is None:
            return False
        try:
            return nursing_page_candidate(page)['source_fingerprint'] == record.source_fingerprint
        except GlucoseSourceUnavailable:
            return False
    if record.source_kind != 'LAB_REPORT' or record.source_observation_id is None:
        return False
    observation = LabObservation.objects.select_related('parsing_version__document', 'document_page', 'evidence').filter(
        pk=record.source_observation_id, parsing_version__document_id=record.source_document_id,
        parsing_version__document__patient_id=record.patient_id).first()
    if observation is None:
        return False
    try:
        return lab_candidate(observation)['source_fingerprint'] == record.source_fingerprint
    except GlucoseSourceUnavailable:
        return False


def lock_nursing_page(patient, page_id):
    from apps.documents.models import DocumentPage

    try:
        document_id = DocumentPage.objects.filter(pk=page_id, document__patient=patient).values_list('document_id', flat=True).first()
    except (ValidationError, ValueError, TypeError):
        raise PermissionDenied from None
    if document_id is None:
        raise PermissionDenied
    document, _ = lock_document_aggregate(document_id, patient_id=patient.pk)
    if document is None:
        raise PermissionDenied
    page = DocumentPage.objects.filter(pk=page_id, document=document).first()
    if page is None:
        raise GlucoseSourceUnavailable('原件页已不可用，请重新打开资料。')
    page.document = document
    return page


def nursing_page_candidate(page):
    version = page.document.parsing_versions.filter(active=True).first()
    source = {**_document_identity(page.document, version), 'page_id': str(page.pk), 'page_number': page.page_number,
              'parsing_version_id': str(version.pk) if version else None, 'page_width': page.width, 'page_height': page.height}
    return {'source': source, 'source_fingerprint': digest(source)}


def preview_nursing_page(patient, actor, page_id):
    with transaction.atomic():
        access = authorize_patient(patient, actor, Capability.READ, lock=True)
        return nursing_page_candidate(lock_nursing_page(access.patient, page_id))


def apply_source_constraints(data):
    if data['source_kind'] == 'NURSING' and data['source'].get('measurement_scope') == 'SUMMARY':
        data['plot_eligible'] = False
        data['unplottable_reason'] = ' '.join(filter(None, [data['unplottable_reason'], '原文是汇总记录，不能作为逐次测量曲线点。']))
    return data
