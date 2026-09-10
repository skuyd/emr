from django.test import Client
import pytest

from apps.cancer_ordering.models import CancerCandidate, CandidateRevision, CollectionRun, DisplaySelection, SelectionRevision
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import revise_candidate
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.access import change_membership
from tests.cancer_ordering.test_services import _collect, _row
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db
INDEX = '/cancer-ordering/'


def _choice(patient, **extra):
    current = resolve_ordering(patient)
    return {'patient_id': str(patient.pk), 'mode': 'GENERAL', 'expected_revision': current['revision_number'],
            'expected_fingerprint': current['fingerprint'], **extra}


def _decision(patient, row, **extra):
    return {'patient_id': str(patient.pk), 'action': 'CONFIRM', 'expected_revision': row['revision_number'],
            'expected_source': row['current_source_token'], 'checked_original': 'on', **extra}


def _url(row):
    return INDEX + 'candidates/' + row['id'] + '/'


def test_read_does_not_create_choice_or_collect_old_records_and_profile_has_entry(django_user_model):
    client, patient = _patient(django_user_model, 'cancer-ui-empty')
    parsed_facts(patient, ['出院诊断：肺癌。'])
    response = client.get(INDEX)
    assert response.status_code == 200
    assert '根据报告自动排列' in response.content.decode()
    assert not DisplaySelection.objects.exists() and not CollectionRun.objects.exists() and not CancerCandidate.objects.exists()
    assert 'no-store' in response['Cache-Control']
    assert INDEX in client.get('/me/').content.decode()
    assert client.get(INDEX + 'collect/').status_code == 405
    assert client.post(INDEX + 'collect/', {'patient_id': str(patient.pk)}).status_code == 303
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def test_family_member_choice_and_undo_record_actual_actor(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'cancer-ui-choice')
    _collect(patient)
    response = client.post(INDEX, _choice(patient, mode='MANUAL_PROFILE', profile='PANCREAS'))
    assert response.status_code == 303
    assert SelectionRevision.objects.get().author_id == actor.pk
    assert resolve_ordering(patient)['profile'] == 'PANCREAS'
    assert client.post(INDEX + 'undo/', _choice(patient)).status_code == 303
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert list(SelectionRevision.objects.order_by('sequence').values_list('action', flat=True)) == ['SELECT', 'UNDO']


def test_candidate_original_check_is_required_and_parent_is_not_confirmed(django_user_model):
    client, patient = _patient(django_user_model, 'cancer-ui-review')
    _, version = _collect(patient)
    row = _row(patient)
    page = client.get(_url(row))
    assert page.status_code == 200 and '/viewer/' in page.content.decode()
    invalid = client.post(_url(row), _decision(patient, row, checked_original=''))
    assert invalid.status_code == 400 and 'checked_original' in invalid.content.decode()
    assert not CandidateRevision.objects.exists()
    assert client.post(_url(row), _decision(patient, row)).status_code == 303
    assert CandidateRevision.objects.get().author_id == patient.account_id
    assert not Fact.objects.get(parsing_version=version).revisions.exists()
    assert client.post(_url(row), _decision(patient, row)).status_code == 409


def test_explicit_candidate_and_invalid_manual_choice_keep_separate_meanings(django_user_model):
    client, patient = _patient(django_user_model, 'cancer-ui-explicit')
    _collect(patient)
    row = _row(patient)
    invalid = client.post(INDEX, _choice(patient, mode='MANUAL_PROFILE', profile='UNSUPPORTED'))
    assert invalid.status_code == 400 and not SelectionRevision.objects.exists()
    assert client.post(INDEX, _choice(patient, mode='CANDIDATE', candidate_id=row['id'])).status_code == 303
    assert resolve_ordering(patient)['mode'] == 'CANDIDATE'
    assert not CandidateRevision.objects.exists()


def test_viewer_has_readable_history_but_no_mutation_form_or_write_capability(django_user_model):
    _, patient, client, _, _ = family(django_user_model, 'cancer-ui-viewer', 'VIEWER')
    _collect(patient)
    row = _row(patient)
    for url in (INDEX, _url(row)):
        response = client.get(url)
        assert response.status_code == 200 and '只读成员' in response.content.decode()
        assert 'name="expected_revision"' not in response.content.decode()
        assert client.post(url, _choice(patient)).status_code == 403
    assert client.post(INDEX + 'collect/', _choice(patient)).status_code == 403
    assert client.post(INDEX + 'undo/', _choice(patient)).status_code == 403


def test_foreign_candidate_cannot_be_used_and_deep_link_resolves_actual_archive(django_user_model):
    from apps.patients.models import Patient

    _, patient, client, actor, _ = family(django_user_model, 'cancer-ui-deep')
    _collect(patient)
    row = _row(patient)
    own = Patient.objects.get(account=actor)
    client.post('/patients/' + str(own.pk) + '/select/')
    assert client.session['active_patient_id'] == str(own.pk)
    response = client.get(_url(row))
    assert response.status_code == 200 and response.context['request'].patient.pk == patient.pk
    foreign, other = _patient(django_user_model, 'cancer-ui-foreign')
    assert foreign.get(_url(row)).status_code == 404
    assert client.get(_url(row), {'patient': str(other.pk)}).status_code == 404
    assert foreign.post(INDEX, _choice(other, mode='CANDIDATE', candidate_id=row['id'])).status_code in {400, 403}
    assert not SelectionRevision.objects.exists()


@pytest.mark.parametrize('page_name', ['index', 'candidate', 'invalid_choice', 'invalid_candidate'])
def test_render_discards_initial_body_and_form_after_parent_change(django_user_model, monkeypatch, page_name):
    from apps.cancer_ordering import views

    client, patient = _patient(django_user_model, 'cancer-ui-source-' + page_name)
    _, version = _collect(patient)
    row = _row(patient)
    fact = Fact.objects.get(parsing_version=version)
    original = views.render
    def changed_after_render(*args, **kwargs):
        response = original(*args, **kwargs)
        revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
        return response
    monkeypatch.setattr(views, 'render', changed_after_render)
    if page_name == 'invalid_choice':
        response = client.post(INDEX, _choice(patient, mode='BAD'))
    elif page_name == 'invalid_candidate':
        response = client.post(_url(row), _decision(patient, row, checked_original=''))
    else:
        response = client.get(INDEX if page_name == 'index' else _url(row))
    assert response.status_code == 409
    assert '出院诊断：肺癌' not in response.content.decode()
    assert not CandidateRevision.objects.exists()


@pytest.mark.parametrize('method', ['get', 'invalid_post'])
def test_membership_revocation_during_render_rejects_even_error_form(django_user_model, monkeypatch, method):
    from apps.cancer_ordering import views

    _, patient, client, _, membership = family(django_user_model, 'cancer-ui-revoke-' + method)
    _collect(patient)
    original = views.render
    def revoke(*args, **kwargs):
        response = original(*args, **kwargs)
        change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        return response
    monkeypatch.setattr(views, 'render', revoke)
    response = client.get(INDEX) if method == 'get' else client.post(INDEX, _choice(patient, mode='BAD'))
    assert response.status_code == 403 and '出院诊断' not in response.content.decode()


def test_csrf_is_required_for_collection_and_selection(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-ui-csrf')
    client = Client(enforce_csrf_checks=True)
    client.force_login(patient.account)
    for url in (INDEX, INDEX + 'collect/'):
        assert client.post(url, {'patient_id': str(patient.pk)}).status_code == 403


def test_history_identifies_actual_choice_after_switching_back_to_auto(django_user_model):
    client, patient = _patient(django_user_model, 'cancer-ui-history')
    _collect(patient)
    assert client.post(INDEX, _choice(patient, mode='MANUAL_PROFILE', profile='PANCREAS')).status_code == 303
    assert client.post(INDEX, _choice(patient, mode='AUTO')).status_code == 303
    response = client.get(INDEX)
    history = response.content.decode().split('显示选择历史', 1)[1]
    assert '手动选择显示顺序' in history and '胰腺癌指标顺序' in history
    assert '根据报告自动排列' in history
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def test_manual_correction_review_actions_keep_original_and_escape_reason(django_user_model):
    client, patient = _patient(django_user_model, 'cancer-ui-correct')
    _, version = _collect(patient, ('出院诊断：胰腺癌。',))
    row = _row(patient, 'PANCREAS')
    original = CancerCandidate.objects.get().original_data
    payload = _decision(patient, row, action='CORRECT', label='肺癌', profile='LUNG',
                        assertion='AFFIRMED', subject='CURRENT_PRIMARY', reason='<script>alert("synthetic")</script>')
    assert client.post(_url(row), payload).status_code == 303
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    detail = client.get(_url(row)).content.decode()
    assert '当前人工更正' in detail and original['raw'] in detail
    assert '<script>alert(' not in detail and '&lt;script&gt;alert' in detail
    for action, expected_status in [('REVOKE', 'PENDING'), ('EXCLUDE', 'EXCLUDED'),
                                    ('UNDO', 'PENDING'), ('DEFER', 'DEFERRED')]:
        current = _row(patient)
        assert client.post(_url(current), _decision(patient, current, action=action, checked_original='')).status_code == 303
        assert _row(patient)['status'] == expected_status
        assert resolve_ordering(patient)['profile'] == 'GENERAL'
    assert CancerCandidate.objects.get().original_data == original
    assert not Fact.objects.get(parsing_version=version).revisions.exists()


def test_old_selection_form_rejects_new_uncollected_report_and_keeps_preference(django_user_model):
    client, patient = _patient(django_user_model, 'cancer-ui-stale-choice')
    _collect(patient)
    payload = _choice(patient, mode='MANUAL_PROFILE', profile='LUNG')
    parsed_facts(patient, ['出院诊断：胰腺癌。'])
    response = client.post(INDEX, payload)
    assert response.status_code == 409 and not SelectionRevision.objects.exists()
    assert resolve_ordering(patient)['reason'] == 'collection_incomplete'
    assert client.post(INDEX, _choice(patient, mode='GENERAL')).status_code == 303
    assert client.post(INDEX, payload).status_code == 409
    assert SelectionRevision.objects.count() == 1
    assert resolve_ordering(patient)['mode'] == 'GENERAL'


def test_trashed_report_is_hidden_and_restored_candidate_requires_review(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document

    client, patient = _patient(django_user_model, 'cancer-ui-trash')
    document, _ = _collect(patient)
    row = _row(patient)
    assert client.post(_url(row), _decision(patient, row)).status_code == 303
    move_to_trash(patient, document.pk, actor=patient.account)
    assert client.get(_url(row)).status_code == 404
    response = client.get(INDEX)
    assert response.status_code == 200 and '出院诊断：肺癌' not in response.content.decode()
    assert all(value != row['id'] for value, _ in response.context['form'].fields['candidate_id'].choices)
    restore_document(patient, document.pk, actor=patient.account)
    response = client.get(_url(row))
    assert response.status_code == 200 and '原确认不再生效' in response.content.decode()
    assert _row(patient)['status'] == 'PENDING'


def test_purged_member_history_is_readable_without_reviving_review_or_choice(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion

    owner, patient, client, actor, _ = family(django_user_model, 'cancer-ui-purged')
    _collect(patient)
    row = _row(patient)
    assert client.post(_url(row), _decision(patient, row)).status_code == 303
    assert client.post(INDEX, _choice(patient, mode='MANUAL_PROFILE', profile='PANCREAS')).status_code == 303
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert purge_account_deletion(job.pk).outcome == 'PURGED'
    for url in (INDEX, _url(row)):
        response = owner.get(url)
        assert response.status_code == 200 and '已注销成员' in response.content.decode()
    assert _row(patient)['status'] == 'PENDING'
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


@pytest.mark.parametrize('invalid_post', [False, True])
def test_new_uncollected_input_during_render_invalidates_initial_page(django_user_model, monkeypatch, invalid_post):
    from apps.cancer_ordering import views

    client, patient = _patient(django_user_model, 'cancer-ui-new-input-' + str(invalid_post))
    _collect(patient)
    original_render = views.render
    def add_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        parsed_facts(patient, ['出院诊断：胰腺癌。'])
        return response
    monkeypatch.setattr(views, 'render', add_after_render)
    response = client.post(INDEX, _choice(patient, mode='BAD')) if invalid_post else client.get(INDEX)
    assert response.status_code == 409 and '出院诊断' not in response.content.decode()
    assert not SelectionRevision.objects.exists()


def test_candidate_reads_denials_and_writes_have_actual_patient_audit_without_duplicate(django_user_model):
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent

    _, patient, client, actor, _ = family(django_user_model, 'cancer-ui-audit')
    _collect(patient)
    row = _row(patient)
    assert client.get(_url(row)).status_code == 200
    read = AuditEvent.objects.get(action='cancer_candidate_viewed', result='succeeded')
    assert read.actor_hash == _hash('actor', actor.pk)
    assert read.patient_hash == _hash('patient', patient.pk)
    assert read.target_hash == _hash('target', row['id']) and read.resource_type == 'cancer_candidate'
    outsider, other = _patient(django_user_model, 'cancer-ui-audit-outsider')
    assert outsider.get(_url(row)).status_code == 404
    denied = AuditEvent.objects.get(action='cancer_candidate_viewed', result='denied')
    assert denied.actor_hash == _hash('actor', other.account_id)
    assert denied.patient_hash == read.patient_hash and denied.target_hash == read.target_hash
    assert client.post(_url(row), _decision(patient, row, checked_original='')).status_code == 400
    assert client.post(_url(row), _decision(patient, row)).status_code == 303
    events = list(AuditEvent.objects.filter(action='cancer_candidate_revised').order_by('created_at'))
    assert [event.result for event in events] == ['failed', 'succeeded']
    assert all(event.actor_hash == read.actor_hash and event.patient_hash == read.patient_hash for event in events)
    assert '肺癌' not in repr(list(AuditEvent.objects.values()))
