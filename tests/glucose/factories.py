from datetime import date

from apps.labs.dictionary import current_dictionary
from apps.labs.models import LabObservation
from apps.processing.models import SourceEvidence
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


def lab_source(django_user_model, *, marker='glucose-source', patient=None, document=None,
               previous=None, value='8.20', unit='mmol/L', raw_name='葡萄糖', specimen='BLOOD',
               title='合成医院检验报告单', sample='2026-08-02 06:12:34'):
    client = None
    if patient is None:
        client, patient = _patient(django_user_model, marker)
    document, version = parsed_facts(patient, [
        title, '标本类型：' + ('血清' if specimen == 'BLOOD' else '中段尿'),
        f'{raw_name} {value} {unit}', '采样时间：' + sample, '报告时间：2026-08-02 09:24:56',
    ], document=document, document_type='LAB', previous=previous)
    row = version.ocr_blocks.get(reading_order=2)
    evidence = SourceEvidence.objects.create(parsing_version=version, document_page=row.document_page,
        polygon=row.polygon, source_text=row.text, confidence='.99')
    fields = {key: {'page_number': 1, 'polygon': row.polygon, 'precision': 'region'}
              for key in ('raw_name', 'raw_value', 'raw_unit', 'observation_date', 'specimen')}
    fields['specimen']['polygon'] = version.ocr_blocks.get(reading_order=1).polygon
    observation = LabObservation.objects.create(parsing_version=version, document_page=row.document_page,
        evidence=evidence, reading_order=2, raw_name=raw_name, standard_code='LAB_FASTING_GLUCOSE',
        standard_name='空腹血糖', raw_value=value, raw_unit=unit, result_type='NUMERIC',
        observation_date=date(2026, 8, 2), specimen=specimen, capability_level='STABLE',
        dictionary_version=current_dictionary().version, field_evidence=fields)
    return client, patient, document, version, observation
