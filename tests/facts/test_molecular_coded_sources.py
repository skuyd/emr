from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_readmodels import effective_field
from tests.facts.molecular_factories import add, context_for, graph
from tests.facts.pathology_factories import review
from tests.facts.test_molecular_context import drug_graph

pytestmark = pytest.mark.django_db
CASES = [('assay.msi_category', 'MSI_L', 'MSI_H', 'MSI-L'),
         ('assay.tmb_qualitative', 'LOW', 'HIGH', '低'),
         ('drug_evidence.direction', 'REPORT_RESISTANCE', 'REPORT_BENEFIT', '原报告耐药')]


def source_graph(model, key):
    if key.startswith('drug_evidence.'):
        patient, _, report, fields, targets, context, source = drug_graph(model)
        targets = {**targets, 'DRUG_EVIDENCE': fields['drugs']}
        return patient, report, fields, targets, context_for(report, targets, deepcopy(context['association'])), source, 'drug_evidence:a'
    _, patient, _, report, fields = graph(model)
    targets = {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}
    return patient, report, fields, targets, context_for(report, targets), '标本甲；检测甲', 'assay:a'


@pytest.mark.parametrize('key,good,bad,raw', CASES)
@pytest.mark.parametrize('mode', ['positive', 'mismatched_code', 'cropped_negation'])
def test_typed_categories_must_match_their_complete_original_window(django_user_model, key, good, bad, raw, mode):
    patient, report, fields, targets, context, source, entity = source_graph(django_user_model, key)
    def create_and_confirm():
        fact = add(patient, report, key, entity, {'code': bad if mode == 'mismatched_code' else good, 'raw': raw}, targets,
            context=context, raw=source + '；' + ('not ' if mode == 'cropped_negation' else '') + raw,
            role='REPORT_DRUG_EVIDENCE' if key.startswith('drug_evidence.') else 'CURRENT_RESULT')
        for item in [*fields.values(), fact]:
            review(patient, item)
        assert effective_field(fact)['usable']
    if mode == 'positive':
        create_and_confirm()
    else:
        with pytest.raises(ValidationError):
            create_and_confirm()


@pytest.mark.parametrize('key,good,bad,raw', CASES)
def test_correction_cannot_invert_a_typed_category_against_the_original(django_user_model, key, good, bad, raw):
    patient, report, fields, targets, context, source, entity = source_graph(django_user_model, key)
    fact = add(patient, report, key, entity, {'code': good, 'raw': raw}, targets, context=context, raw=source + '；' + raw,
        role='REPORT_DRUG_EVIDENCE' if key.startswith('drug_evidence.') else 'CURRENT_RESULT')
    for item in [*fields.values(), fact]:
        review(patient, item)
    assert effective_field(fact)['usable']
    with pytest.raises(ValidationError):
        review(patient, fact, 'CORRECT', {'value': {'code': bad, 'raw': raw}, 'raw_value': fact.raw_text})
        assert effective_field(fact)['usable']


@pytest.mark.parametrize('key', ['assay.msi_category', 'assay.tmb_qualitative'])
@pytest.mark.parametrize('cropped', [False, True])
def test_unclassified_category_preserves_complete_original_without_claiming_report_uncertainty(django_user_model, key, cropped):
    patient, report, fields, targets, context, source, entity = source_graph(django_user_model, key)
    original = 'not high SYN extended category' if not cropped else 'not high'
    raw = 'high' if cropped else original
    def create():
        return add(patient, report, key, entity, {'code': 'UNKNOWN', 'raw': raw}, targets, context=context, raw=source + '；' + original)
    if cropped:
        with pytest.raises(ValidationError):
            create()
    else:
        fact = create()
        for item in [*fields.values(), fact]:
            review(patient, item)
        value = effective_field(fact)['content']['value']
        assert value == {'code': 'UNKNOWN', 'raw': original}


@pytest.mark.parametrize('raw,code', [('none resistance', 'REPORT_RESISTANCE'), ('absence of resistance', 'REPORT_RESISTANCE'),
    ('not uncertain', 'UNCERTAIN'), ('unclassified direction', 'NOT_STATED'), ('directionresistance', 'REPORT_RESISTANCE')])
def test_unmapped_or_negated_drug_direction_is_not_reclassified_as_a_report_claim(django_user_model, raw, code):
    key = 'drug_evidence.direction'
    patient, report, _, targets, context, source, entity = source_graph(django_user_model, key)
    with pytest.raises(ValidationError):
        add(patient, report, key, entity, {'code': code, 'raw': raw}, targets, context=context, raw=source + '；' + raw,
            role='REPORT_DRUG_EVIDENCE')


@pytest.mark.parametrize('negated', [False, True])
def test_automatic_category_gate_uses_real_uncropped_ocr_and_retains_other_results(django_user_model, negated):
    from tests.facts.test_molecular_pipeline import fixture, report_rows
    from tests.facts.test_clinical_segments import block
    text = ('not\n' if negated else '') + 'MSI类别：MSI-L'
    rows = report_rows()[:9] + [block(text, order=20)]
    patient, document, version, result = fixture(django_user_model, rows)
    report = document.clinical_reports.get()
    assert report.fields.filter(field_key='variant.identity').exists()
    assert version.ocr_blocks.get(reading_order=len(rows)-1).text == text
    if negated:
        assert not report.fields.filter(field_key='assay.msi_category').exists()
        assert 'unclassified_molecular_category_source' in result.limitations
    else:
        field = report.fields.get(field_key='assay.msi_category')
        assert field.automatic_content['value'] == {'code': 'MSI_L', 'raw': 'MSI-L'}


@pytest.mark.parametrize('key,good,bad,raw', CASES)
def test_existing_mismatched_typed_revision_is_never_a_current_usable_result(django_user_model, key, good, bad, raw):
    from apps.facts.models import Fact, FactRevision
    patient, report, fields, targets, context, source, entity = source_graph(django_user_model, key)
    fact = add(patient, report, key, entity, {'code': good, 'raw': raw}, targets, context=context, raw=source + '；' + raw,
        role='REPORT_DRUG_EVIDENCE' if key.startswith('drug_evidence.') else 'CURRENT_RESULT')
    for item in [*fields.values(), fact]:
        review(patient, item)
    prior = fact.revisions.latest('sequence')
    after = deepcopy(prior.after)
    after['content']['value']['code'] = bad
    # Persist the exact shape of an older, previously accepted correction. The
    # reader must fail closed even though new correction requests are rejected.
    FactRevision.objects.create(fact=fact, author=patient.account, sequence=prior.sequence + 1, action='CORRECT',
        before=deepcopy(prior.after), after=after, source=deepcopy(prior.source))
    Fact.objects.filter(pk=fact.pk).update(revision_number=prior.sequence + 1)
    fact.refresh_from_db()
    assert not effective_field(fact)['usable']
    assert fact.automatic_content['value']['code'] == good


@pytest.mark.parametrize('negated', [False, True])
def test_explicit_table_categories_use_whole_original_cells_under_real_headers(django_user_model, negated):
    from tests.facts.test_molecular_pipeline import fixture, report_rows, confirm_report
    from tests.facts.test_clinical_segments import block
    prefix = 'not ' if negated else ''
    rows = report_rows()[:9] + [block('MSI类别|TMB定性\n' + prefix + 'MSI-L|' + prefix + '低', order=20)]
    patient, document, _, result = fixture(django_user_model, rows)
    report = document.clinical_reports.get()
    confirm_report(patient, report)
    for key, raw, code in [('assay.msi_category', 'MSI-L', 'MSI_L'), ('assay.tmb_qualitative', '低', 'LOW')]:
        fact = report.fields.get(field_key=key)
        assert fact.automatic_content['value'] == {'raw': prefix + raw, 'code': 'UNKNOWN' if negated else code}
        assert effective_field(fact)['usable']


@pytest.mark.parametrize('key,raw,code', [('assay.msi_category', 'MSI-L', 'MSI_L'), ('assay.tmb_qualitative', '低', 'LOW')])
def test_automatic_table_correction_cannot_crop_a_cells_negation(django_user_model, key, raw, code):
    from tests.facts.test_molecular_pipeline import fixture, report_rows, confirm_report
    from tests.facts.test_clinical_segments import block
    patient, document, _, _ = fixture(django_user_model, report_rows()[:9] + [block('MSI类别|TMB定性\nnot MSI-L|not 低', order=20)])
    report = document.clinical_reports.get()
    confirm_report(patient, report)
    fact = report.fields.get(field_key=key)
    assert effective_field(fact)['usable']
    with pytest.raises(ValidationError):
        review(patient, fact, 'CORRECT', {'value': {'code': code, 'raw': raw}, 'raw_value': fact.raw_text})
    assert effective_field(fact)['content']['value'] == {'code': 'UNKNOWN', 'raw': 'not ' + raw}


@pytest.mark.parametrize('prefix,crop', [('', False), *[(word, crop) for word in ('未', '不能', '不支持', '不能排除') for crop in (False, True)]])
def test_existing_report_resistance_phrase_keeps_full_reported_meaning(django_user_model, prefix, crop):
    key = 'drug_evidence.direction'
    patient, report, fields, targets, context, source, entity = source_graph(django_user_model, key)
    original = prefix + '报告耐药'
    def add_and_confirm():
        fact = add(patient, report, key, entity, {'code': 'REPORT_RESISTANCE', 'raw': '报告耐药' if crop else original},
            targets, context=context, raw=source + '；' + original, role='REPORT_DRUG_EVIDENCE')
        for field in [*fields.values(), fact]:
            review(patient, field)
        assert effective_field(fact)['usable']
        assert effective_field(fact)['content']['value']['raw'] == original
    if prefix:
        with pytest.raises(ValidationError):
            add_and_confirm()
    else:
        add_and_confirm()
