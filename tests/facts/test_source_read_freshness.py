"""The released HTML must belong to the source and history used to build it."""
from django.test import Client
import pytest

from apps.facts.readmodels import effective_fact
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_laterality_scope_operations import replace
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


def source_page(django_user_model, route, name):
    """Real source, services, form and renderer for each affected entry family."""
    from apps.facts import views as fact_views
    from apps.lesions import views as lesion_views
    from apps.lesions.readmodels import observation_material
    from apps.lesions.services import create_lesion

    patient, report, parent, child = fixture(django_user_model, name)
    confirm(patient, parent)
    confirm(patient, child)
    views, status, method = fact_views, 200, 'GET'
    if route == 'scope_operation':
        event = replace(patient, parent, child, confirm_new=True)
        url, template = f'/facts/scope-operations/{event.pk}/', 'facts/scope_operation.html'
    elif route in {'scope_get', 'scope_invalid_post'}:
        url, template = f'/facts/{child.pk}/scope/', 'facts/scope_change.html'
    elif route == 'field':
        url, template = f'/facts/{child.pk}/', 'facts/field.html'
    elif route == 'report':
        url, template = f'/facts/reports/{report.pk}/', 'facts/report.html'
    else:
        row = next(r for r in observation_material(patient) if r['report_id'] == str(report.pk))
        event = create_lesion(patient, actor=patient.account, observation_id=row['id'],
            expected_revision=row['revision_number'], expected_source=row['source_token'],
            checked_original=True, name='Source-bound synthetic observation')
        lesion_id = event.lesion_revisions.get().lesion_id
        views = lesion_views
        if route == 'lesion_detail':
            url, template = f'/lesions/{lesion_id}/', 'lesions/detail.html'
        elif route == 'lesion_operation':
            url, template = f'/lesions/operations/{event.pk}/', 'lesions/operation.html'
        elif route == 'lesion_match_invalid_post':
            url, template = '/lesions/match/', 'lesions/select.html'
        else:
            raise AssertionError('Unrecognized source-page test case')
    if route.endswith('invalid_post'):
        method, status = 'POST', 400
    parent.refresh_from_db()
    client = Client()
    client.force_login(patient.account)
    return patient, parent, client, views, url, template, method, status


def fetch_page(client, patient, method, url):
    return client.post(url, {'patient_id': str(patient.pk), 'action': 'REPLACE'}) if method == 'POST' else client.get(url)


@pytest.mark.parametrize('route', ['scope_get', 'scope_invalid_post', 'scope_operation', 'field', 'report',
                                   'lesion_detail', 'lesion_operation', 'lesion_match_invalid_post'])
@pytest.mark.parametrize('change', [False, True])
def test_source_and_history_pages_drop_body_after_parent_revision(django_user_model, monkeypatch, route, change):
    patient, parent, client, views, url, target, method, unchanged_status = source_page(
        django_user_model, route, f'read-freshness-{route}-{change}')
    original, rendered = views.render, []
    def render_then_change(request, template, context, **kwargs):
        response = original(request, template, context, **kwargs)
        if template == target:
            rendered.append(response.content)
            if change:
                confirm(patient, parent, 'REVOKE')
        return response
    monkeypatch.setattr(views, 'render', render_then_change)
    response = fetch_page(client, patient, method, url)
    assert rendered
    if change:
        parent.refresh_from_db()
        assert not effective_fact(parent)['usable']
        assert response.status_code == 409
        assert response.content != rendered[0]
    else:
        assert response.status_code == unchanged_status
        assert response.content == rendered[0]


def test_old_choices_cannot_be_certified_by_a_snapshot_taken_only_at_render(django_user_model, monkeypatch):
    from apps.facts.laterality_forms import ScopeChangeForm
    patient, parent, client, _, url, _, _, _ = source_page(django_user_model, 'scope_get', 'scope-old-choices')
    original = ScopeChangeForm.__init__
    def construct_then_change(self, *args, **kwargs):
        original(self, *args, **kwargs)
        confirm(patient, parent, 'REVOKE')
    monkeypatch.setattr(ScopeChangeForm, '__init__', construct_then_change)
    response = client.get(url)
    assert response.status_code == 409
    assert b'members-TOTAL_FORMS' not in response.content


@pytest.mark.parametrize('kind', ['scope_operation', 'lesion_history'])
def test_purging_actual_intermediate_author_discards_already_rendered_history(django_user_model, monkeypatch, kind):
    from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
    from apps.patients.models import PatientMembership
    from apps.lesions.models import Lesion
    from apps.lesions.services import rename_lesion
    from tests.documents.test_detail_viewer import _patient
    route = 'scope_get' if kind == 'scope_operation' else 'lesion_detail'
    patient, parent, client, views, url, target, _, _ = source_page(django_user_model, route, f'read-author-{kind}')
    _, own = _patient(django_user_model, f'read-author-editor-{kind}')
    editor = own.account
    PatientMembership.objects.create(patient=patient, account=editor, role='EDITOR')
    if kind == 'scope_operation':
        child = parent.clinical_report.fields.get(field_key='lesion.scoped_laterality')
        event = replace(patient, parent, child, actor=editor, confirm_new=True)
        url, target = f'/facts/scope-operations/{event.pk}/', 'facts/scope_operation.html'
    else:
        lesion = Lesion.objects.get(patient=patient)
        rename_lesion(patient, actor=editor, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='Earlier author name')
        lesion.refresh_from_db()
        rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='Current owner name')
    original, rendered = views.render, []
    def render_then_purge(request, template, context, **kwargs):
        response = original(request, template, context, **kwargs)
        if template == target:
            rendered.append(response.content)
            assert str(editor.pk).encode() in response.content
            deletion = request_account_deletion(editor.pk, document_dispatch=lambda *_: None, account_dispatch=lambda *_: None)
            assert purge_account_deletion(deletion.pk).outcome == AccountDeletionOutcome.PURGED
            assert not django_user_model.objects.filter(pk=editor.pk).exists()
        return response
    monkeypatch.setattr(views, 'render', render_then_purge)
    response = client.get(url)
    assert rendered and response.status_code == 409
    assert str(editor.pk).encode() not in response.content


@pytest.mark.parametrize('change', ['revoke', 'downgrade'])
def test_permission_is_rechecked_after_the_final_source_read(django_user_model, monkeypatch, change):
    from apps.facts import read_guards
    from apps.patients.access import change_membership
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient

    patient, _, client, _, url, _, method, _ = source_page(
        django_user_model, 'scope_invalid_post', 'final-permission-' + change)
    _, own = _patient(django_user_model, 'final-permission-editor-' + change)
    member = PatientMembership.objects.create(patient=patient, account=own.account, role='EDITOR')
    client.force_login(own.account)
    original, snapshots = read_guards.source_snapshot, []
    def read_then_change(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshots.append(snapshot)
        if len(snapshots) == 2:
            change_membership(patient, patient.account, member.pk,
                expected_revision=member.revision, revoke=change == 'revoke',
                role='VIEWER' if change == 'downgrade' else None)
        return snapshot
    monkeypatch.setattr(read_guards, 'source_snapshot', read_then_change)
    response = fetch_page(client, patient, method, url)
    assert len(snapshots) == 2
    assert response.status_code in {403, 409}
    assert b'members-TOTAL_FORMS' not in response.content
