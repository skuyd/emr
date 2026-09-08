"""Parent changes must invalidate evidence without erasing a user's decision."""
from copy import deepcopy
import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
import pytest

from apps.cancer_ordering.models import CancerCandidate, CollectionRun, NarrativeDependency, NarrativeSource
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current, OrderingConflict
from apps.documents.models import DocumentPage
from apps.facts.models import Fact
from apps.facts.revisions import add_manual_fact, revise_fact
from apps.patients.models import PatientMembership
from apps.processing.models import OcrBlock, ParsingVersion
from tests.cancer_ordering.test_services import _revise
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def narrative_case(model, name='narrative-dependency'):
    _, patient = _patient(model, name)
    document, version = parsed_facts(patient, ['现病史：患者诊断为肺癌，已行化疗。'])
    parent = Fact.objects.get(parsing_version=version)
    assert parent.category == 'TREATMENT'
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(patient=patient)
    assert candidate.source_narrative_id and not candidate.source_fact_id
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    return patient, document, version, parent, candidate


def current_row(patient):
    row, = candidate_rows(patient)
    return row


@pytest.mark.parametrize('decision', ['EXCLUDE', 'DEFER', 'CORRECT_REVOKE'])
@pytest.mark.parametrize('parent_change', ['confirm', 'add', 'remove', 'scope_addition'])
def test_parent_generations_cannot_reset_original_user_barriers(django_user_model, decision, parent_change):
    patient, document, version, parent, candidate = narrative_case(django_user_model, decision + parent_change)
    original = deepcopy(candidate.original_source)
    if decision == 'CORRECT_REVOKE':
        _revise(patient, current_row(patient), 'CORRECT', checked_original=True, reason='合成原件核对', changes={
            'label': '肺癌', 'profile': 'LUNG', 'assertion': 'UNCERTAIN', 'subject': 'CURRENT_PRIMARY'})
        _revise(patient, current_row(patient), 'REVOKE')
    else:
        _revise(patient, current_row(patient), decision)
    history = list(candidate.revisions.values())
    if parent_change == 'confirm':
        revise_fact(patient, parent.pk, actor=patient.account, action='CONFIRM', expected_revision=0, checked_original=True)
    elif parent_change == 'remove':
        parent.delete()
    else:
        add_manual_fact(patient, document.pk, actor=patient.account, page_number=1, category='DIAGNOSIS',
                        text='患者诊断为肺癌。' if parent_change == 'add' else '现病史：患者诊断为肺癌，已行化疗。')
    collect_current(patient, actor=patient.account)
    candidate.refresh_from_db()
    row = next(row for row in candidate_rows(patient) if row['id'] == str(candidate.pk))
    assert row['source_changed'] and not row['eligible_for_auto']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    assert list(candidate.revisions.values()) == history and candidate.original_source == original
    assert CancerCandidate.objects.filter(source_narrative__isnull=False, patient=patient).count() == 1
    assert NarrativeSource.objects.filter(document=document).count() == 1


def test_parent_tombstone_preserves_captured_identity_and_blocks_recollect(django_user_model):
    patient, _, _, parent, candidate = narrative_case(django_user_model, 'narrative-parent-tombstone')
    dependency = NarrativeDependency.objects.get(narrative_source=candidate.source_narrative)
    original_id, captured = parent.pk, deepcopy(dependency.input_snapshot)
    assert dependency.fact_id == original_id and dependency.position_status == 'EXACT'
    parent.delete()
    dependency.refresh_from_db()
    assert dependency.fact_id is None and dependency.original_fact_id == original_id
    assert dependency.input_snapshot == captured
    collect_current(patient, actor=patient.account)
    assert not current_row(patient)['source_valid']
    assert NarrativeDependency.objects.filter(original_fact_id=original_id, position_status='MISSING').exists()
    with pytest.raises(OrderingConflict):
        _revise(patient, current_row(patient), 'CONFIRM', checked_original=True)


def test_parent_correction_cannot_supply_ocr_auto_after_candidate_revoke(django_user_model):
    patient, _, _, parent, candidate = narrative_case(django_user_model, 'narrative-parent-correction')
    revise_fact(patient, parent.pk, actor=patient.account, action='CORRECT', expected_revision=0,
                checked_original=True, changes={'text': '现病史：患者诊断为胰腺癌，已行化疗。'})
    collect_current(patient, actor=patient.account)
    assert current_row(patient)['source_changed'] and resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, current_row(patient), 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    _revise(patient, current_row(patient), 'REVOKE')
    assert not current_row(patient)['manual_correction']
    assert not current_row(patient)['eligible_for_auto'] and resolve_ordering(patient)['profile'] == 'GENERAL'
    candidate.refresh_from_db()
    assert candidate.original_data['label'] == '肺癌'


@pytest.mark.parametrize('decision', ['CONFIRM', 'EXCLUDE', 'DEFER', 'CORRECT_REVOKE'])
def test_real_source_generation_retains_occurrence_history_without_inheriting_confirmation(django_user_model, monkeypatch, decision):
    from apps.cancer_ordering import narrative_layout
    patient, document, _, _, candidate = narrative_case(django_user_model, 'narrative-rule-' + decision)
    if decision == 'CORRECT_REVOKE':
        _revise(patient, current_row(patient), 'CORRECT', checked_original=True, reason='合成核对', changes={
            'label': '肺癌', 'profile': 'LUNG', 'assertion': 'AFFIRMED', 'subject': 'CURRENT_PRIMARY'})
        _revise(patient, current_row(patient), 'REVOKE')
    else:
        _revise(patient, current_row(patient), decision, checked_original=decision == 'CONFIRM')
    prior = list(candidate.revisions.values())
    monkeypatch.setattr(narrative_layout, 'LAYOUT_VERSION', 'synthetic-narrative-layout-next')
    assert not resolve_ordering(patient)['complete']
    collect_current(patient, actor=patient.account)
    assert NarrativeSource.objects.filter(document=document).count() == 2
    assert CancerCandidate.objects.get(patient=patient).pk == candidate.pk
    assert list(candidate.revisions.values()) == prior
    row = current_row(patient)
    assert row['source_changed'] and row['status'] != 'CONFIRMED' and not row['eligible_for_auto']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_historical_parent_author_purge_requires_new_review_with_latest_author_still_active(django_user_model):
    patient, _, _, parent, _ = narrative_case(django_user_model, 'narrative-history-author')
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(b'narrative-history-author-editor').hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    revise_fact(patient, parent.pk, actor=actor, action='DEFER', expected_revision=0)
    revise_fact(patient, parent.pk, actor=patient.account, action='CONFIRM', expected_revision=1, checked_original=True)
    collect_current(patient, actor=patient.account)
    _revise(patient, current_row(patient), 'CONFIRM', checked_original=True)
    before = current_row(patient)
    old_id = str(actor.pk)
    actor.delete()
    after = current_row(patient)
    assert after['source_changed'] and after['current_source_token'] != before['current_source_token']
    assert old_id not in str(after)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_source_choice_xor_is_enforced_by_database_and_old_fact_rows_still_work(django_user_model):
    patient, document, _, parent, candidate = narrative_case(django_user_model, 'narrative-db-xor')
    fields = dict(patient=patient, document=document, occurrence_key='a' * 64, rule_version='synthetic',
                  original_data={'synthetic': True}, original_source={'synthetic': True})
    for sources in ({}, {'source_fact': parent, 'source_narrative': candidate.source_narrative}):
        with pytest.raises(IntegrityError), transaction.atomic():
            CancerCandidate.objects.create(**fields, **sources)
    legacy = CancerCandidate.objects.create(**fields, source_fact=parent)
    assert legacy.source_narrative_id is None


@pytest.mark.parametrize('change', ['page', 'version', 'offset', 'polygon', 'confidence'])
def test_narrative_binding_rejects_wrong_original_identity(django_user_model, change):
    patient, document, _, _, candidate = narrative_case(django_user_model, 'narrative-binding-' + change)
    source = candidate.source_narrative
    source.clean()
    if change == 'page':
        source.document_page = DocumentPage.objects.create(document=document, page_number=2)
    elif change == 'version':
        _, source.parsing_version = parsed_facts(patient, ['主诉：肺癌。'], document=document)
    elif change == 'offset':
        source.original_source['label_fragments'][0]['start'] += 1
    elif change == 'polygon':
        source.original_source['label_fragments'][0]['polygon'][0][0] = .001
    else:
        source.original_source['label_fragments'][0]['confidence'] = '.10'
    with pytest.raises(ValidationError):
        source.clean()


def test_missing_page_and_low_heading_confidence_never_become_unconditional_auto(django_user_model):
    patient, document, version, _, _ = narrative_case(django_user_model, 'narrative-partial-page')
    DocumentPage.objects.create(document=document, page_number=2)
    collect_current(patient, actor=patient.account)
    failed = CollectionRun.objects.filter(parsing_version=version).order_by('-sequence').first()
    assert failed.status == 'FAILED' and failed.error_code == 'narrative_scope_incomplete'
    assert not resolve_ordering(patient)['complete']
    assert any('no_ocr' in row['reasons'] for row in failed.input_snapshot['narratives']['coverage'])


def test_read_and_viewer_collection_cannot_materialize_unreviewed_sources(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-read-only')
    parsed_facts(patient, ['主诉：肺癌。'], document_type='UNKNOWN')
    viewer = django_user_model.objects.create(phone_hash=hashlib.sha256(b'narrative-viewer').hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=viewer, role='VIEWER')
    assert not resolve_ordering(patient)['complete']
    assert not NarrativeSource.objects.exists() and not CollectionRun.objects.exists()
    with pytest.raises(PermissionDenied):
        collect_current(patient, actor=viewer)
    assert not NarrativeSource.objects.exists()


def test_persisted_context_keeps_original_character_map_including_unmapped_wraps(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-character-map')
    _, version = parsed_facts(patient, ['主诉：', '肺癌；肺癌。'], document_type='UNKNOWN')
    collect_current(patient, actor=patient.account)
    for source in NarrativeSource.objects.filter(document__patient=patient):
        mapping = source.original_source['character_map']
        assert len(mapping) == len(source.raw_text)
        for char, point in zip(source.raw_text, mapping):
            if point is None:
                assert char.isspace()
            else:
                block = OcrBlock.objects.get(pk=point[0], parsing_version=version)
                assert block.text[point[1]] == char
        start, end = source.original_data['match_start'], source.original_data['match_end']
        label_points = [[fragment['block_id'], offset] for fragment in source.original_source['label_fragments']
                        for offset in range(fragment['start'], fragment['end'])]
        assert [point for point in mapping[start:end] if point is not None] == label_points
    assert NarrativeSource.objects.filter(document__patient=patient).count() == 2


def test_manual_parent_from_previous_parse_stays_a_real_page_dependency(django_user_model):
    patient, document, version, _, _ = narrative_case(django_user_model, 'narrative-old-manual')
    manual = add_manual_fact(patient, document.pk, actor=patient.account, page_number=1, category='TREATMENT',
                             text='现病史：患者诊断为肺癌，已行化疗。')
    _, new_version = parsed_facts(patient, ['现病史：患者诊断为肺癌，已行化疗。'], document=document, previous=version)
    collect_current(patient, actor=patient.account)
    latest = CollectionRun.objects.filter(parsing_version=new_version).order_by('-sequence').first()
    assert latest.status == 'COMPLETE'
    dependency = NarrativeDependency.objects.get(collection=latest, original_fact_id=manual.pk)
    assert dependency.fact_id == manual.pk and dependency.position_status == 'UNVERIFIED'
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_a_current_source_generation_can_be_reviewed_without_rewriting_its_predecessor(django_user_model):
    patient, document, version, _, candidate = narrative_case(django_user_model, 'narrative-new-proof')
    original = deepcopy(candidate.source_narrative.original_source)
    _revise(patient, current_row(patient), 'CONFIRM', checked_original=True)
    OcrBlock.objects.filter(parsing_version=version).update(confidence='.9490')
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    collect_current(patient, actor=patient.account)
    assert NarrativeSource.objects.filter(document=document).count() == 2
    current = current_row(patient)
    assert current['source_valid'] and current['source_changed'] and not current['eligible_for_auto']
    _revise(patient, current, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    candidate.source_narrative.refresh_from_db()
    assert candidate.source_narrative.original_source == original
