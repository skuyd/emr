"""Source-bound manual transcription and its parsing-version lineage."""

from collections import defaultdict
from copy import copy
from uuid import uuid4
import unicodedata

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.db.models import Q

from apps.processing.models import SourceEvidence

from .dictionary import dictionary_for_version
from .extraction import _candidate_identity, _result_type
from .models import CapabilityLevel, LabObservation, ResultType
from .validation import VALIDATION_RULE_VERSION, parse_reference_range
from tools.sample_dictionary.normalize import normalize_candidate_name


_FIELDS = {'raw_name': 256, 'raw_value': 256, 'raw_unit': 64, 'reference_range_raw': 512,
           'report_flag_raw': 32, 'specimen': 32, 'method_raw': 256}


def add_manual_observation(unit, author, values):
    """Caller holds the patient/document/report locks and checked this unit's scope."""
    if not isinstance(values, dict) or set(values) - set(_FIELDS):
        raise ValidationError('补录指标字段无效。')
    cleaned = {}
    for name, limit in _FIELDS.items():
        value = values.get(name, '')
        if (not isinstance(value, str) or len(value.strip()) > limit
                or any(unicodedata.category(character).startswith('C') for character in value)):
            raise ValidationError(f'{name} 的原文无效。')
        cleaned[name] = value.strip()
    if not cleaned['raw_name'] or not cleaned['raw_value']:
        raise ValidationError('补录指标必须填写原项目名称和原文结果。')
    version = unit.parsing_version
    if not version.active or not version.dictionary_version:
        raise ValidationError('当前报告没有可用解析版本，暂不能补录。')
    dictionary = dictionary_for_version(version.dictionary_version)
    normalized_name = normalize_candidate_name(cleaned['raw_name'], strip_result=False)
    definition, code, standard_name, capability = _candidate_identity(
        normalized_name, dictionary, specimen=cleaned['specimen'])
    kind = _result_type(cleaned['raw_value']) or ResultType.STATUS
    source = SourceEvidence(parsing_version=version, document_page=unit.document_page,
                            source_text=f"{cleaned['raw_name']} {cleaned['raw_value']}",
                            origin='MANUAL', confidence=None, polygon=None)
    source.full_clean()
    source.save()
    order = (LabObservation.objects.filter(parsing_version=version, document_page=unit.document_page)
             .aggregate(last=Max('reading_order'))['last'] or 0) + 1
    field_evidence = {name: {'page_number': unit.document_page.page_number, 'polygon': None,
                             'precision': 'page', 'origin': 'MANUAL', 'text': value}
                      for name, value in cleaned.items() if value}
    row = LabObservation(parsing_version=version, document_page=unit.document_page, evidence=source,
        report_unit=unit, reading_order=order, manual_identity=uuid4(), manual_created_by=author,
        raw_name=cleaned['raw_name'], raw_value=cleaned['raw_value'], raw_unit=cleaned['raw_unit'],
        reference_range_raw=cleaned['reference_range_raw'], report_flag_raw=cleaned['report_flag_raw'],
        specimen=cleaned['specimen'], method_raw=cleaned['method_raw'],
        standard_code=code, standard_name=standard_name, result_type=kind,
        capability_level=capability if definition else CapabilityLevel.SEARCH_ONLY,
        dictionary_version=version.dictionary_version, reference_range=parse_reference_range(cleaned['reference_range_raw']),
        quality_rule_version=VALIDATION_RULE_VERSION, field_evidence=field_evidence)
    row.full_clean()
    row.save()
    return row


def carry_manual_observations(target):
    """Called within parsing activation after the new report units exist."""
    from .reports import effective_report
    from .revisions import VALUE_FIELDS, effective_observation

    previous = target.previous_version
    if previous is None:
        return
    originals = (LabObservation.objects.filter(parsing_version=previous).filter(
                    Q(manual_identity__isnull=False) | Q(manual_counterpart__isnull=False))
                 .select_related('evidence', 'report_unit', 'document_page', 'manual_created_by'))
    lineages = defaultdict(list)
    for old in originals:
        lineages[old.manual_identity or old.manual_counterpart].append(old)
    if not lineages:
        return
    units = {unit.source_key: unit for unit in target.lab_report_units.select_related('document_page')}
    for identity, ancestors in lineages.items():
        choices = [(row, effective_observation(row)) for row in ancestors]
        retained = [(row, effective) for row, effective in choices
                    if not effective.excluded and not effective.manual_conflict]
        old, effective = (retained[0] if len(retained) == 1 else
                          next(((row, item) for row, item in choices if row.manual_identity == identity), choices[0]))
        if old.report_unit_id is None or old.report_unit.source_key not in units:
            continue
        if LabObservation.objects.filter(parsing_version=target, manual_identity=identity).exists():
            continue
        unit = units[old.report_unit.source_key]
        moved = effective_report(old.report_unit).source_region != effective_report(unit).source_region
        candidates = list(LabObservation.objects.filter(parsing_version=target, report_unit=unit,
            manual_identity__isnull=True, manual_counterpart__isnull=True,
            evidence__origin='AUTOMATIC', raw_name=effective.raw_name).select_related('evidence'))
        exact = [item for item in candidates if all(getattr(item, field) == getattr(effective, field)
            for field in ('raw_value', 'raw_unit', 'reference_range_raw', 'report_flag_raw',
                          'specimen', 'method_raw'))]
        manual_source = (LabObservation.objects.filter(manual_identity=identity,
                         evidence__origin='MANUAL').select_related('evidence').order_by('created_at').first())
        creator = manual_source.manual_created_by if manual_source else old.manual_created_by
        if len(candidates) == len(exact) == 1 and not moved and not effective.manual_conflict:
            matched = exact[0]
            matched.manual_identity = identity
            matched.manual_created_by = creator
            matched.save(update_fields=['manual_identity', 'manual_created_by'])
            continue
        conflict = effective.manual_conflict or ('SOURCE' if moved else 'VALUE' if candidates else '')
        if conflict:
            LabObservation.objects.filter(pk__in=[item.pk for item in candidates]).update(
                manual_conflict=conflict, manual_counterpart=identity, manual_created_by=creator)
        evidence = SourceEvidence(parsing_version=target, document_page=unit.document_page,
            origin='MANUAL', source_text=(manual_source.evidence.source_text if manual_source else
                                          old.evidence.source_text), confidence=None, polygon=None)
        evidence.full_clean()
        evidence.save()
        order = (LabObservation.objects.filter(parsing_version=target, document_page=unit.document_page)
                 .aggregate(last=Max('reading_order'))['last'] or 0) + 1
        dictionary = dictionary_for_version(target.dictionary_version)
        definition, code, standard_name, capability = _candidate_identity(
            normalize_candidate_name(effective.raw_name, strip_result=False), dictionary,
            specimen=effective.specimen)
        current = copy(old)
        current.pk = uuid4()
        current.manual_identity = identity
        current.manual_counterpart = None
        current.manual_created_by = creator
        current.parsing_version = target
        current.document_page = unit.document_page
        current.evidence = evidence
        current.report_unit = unit
        current.reading_order = order
        current.revision_number = 0
        current.manual_conflict = conflict
        current.dictionary_version = target.dictionary_version
        current.standard_code = code
        current.standard_name = standard_name
        current.capability_level = capability if definition else CapabilityLevel.SEARCH_ONLY
        current.reference_range = parse_reference_range(effective.reference_range_raw)
        current.quality_issues = []
        current.normalization_candidates = []
        for field in VALUE_FIELDS:
            if field in {'standard_code', 'standard_name', 'capability_level'}:
                continue
            setattr(current, field, getattr(effective, field))
        current.field_evidence = {name: {'page_number': unit.document_page.page_number, 'polygon': None,
                                  'precision': 'page', 'origin': 'MANUAL', 'text': str(getattr(current, name))}
                                  for name in _FIELDS if getattr(current, name)}
        current.full_clean()
        current.save(force_insert=True)
