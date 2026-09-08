"""Actual output boundaries to run after shared glucose wiring lands locally."""
from uuid import uuid4

import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.exports.errors import ExportUnavailable
from apps.exports.services import create_preview, generate_export, get_preview, request_generation
from apps.glucose.services import create_record, revise_record
from apps.patients.sharing import create_share, exchange_share_token
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.glucose.test_payloads import payload
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def selection(record):
    return {'mode': 'documents', 'document_ids': [], 'glucose_record_ids': [str(record.pk)],
            'sections': ['patient', 'glucose'], 'details': True}


def purge(actor):
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED


@pytest.mark.parametrize('method', ['GET', 'invalid_POST'])
@pytest.mark.parametrize('change', ['revision', 'author_purge', 'unchanged'])
def test_actual_preview_html_rechecks_sources_after_render(django_user_model, monkeypatch, method, change):
    from apps.exports import views

    owner, patient, _, author, _ = family(django_user_model, 'glucose-preview-' + method + change)
    author_id = str(author.pk)
    marker = 'SYNTHETIC SELECTED GLUCOSE PREVIEW'
    record = create_record(patient, author, payload(notes=marker), creation_key=uuid4()).record
    assert owner.get('/records/').status_code == 200
    job = create_preview(patient, owner.session.session_key, selection(record), actor=patient.account)
    render = views._render
    observed = []
    rendered_status = 200 if method == 'GET' else 400

    def render_then_change(request, template, context=None, *args, **kwargs):
        response = render(request, template, context, *args, **kwargs)
        if template == 'exports/preview.html':
            assert marker.encode() in response.content
            assert author_id.encode() in response.content
            observed.append(response.status_code)
            if change == 'revision':
                revise_record(patient, author, record.pk, action='CORRECT', expected_revision=0,
                              changes=payload(value='6.70'))
            elif change == 'author_purge':
                purge(author)
        return response

    monkeypatch.setattr(views, '_render', render_then_change)
    response = (owner.get(f'/visit/{job.pk}/') if method == 'GET'
                else owner.post(f'/visit/{job.pk}/', {'format': 'invalid-format'}))
    assert observed == [rendered_status]
    assert response['Cache-Control'] == 'private, no-store, max-age=0'
    if change == 'unchanged':
        assert response.status_code == rendered_status
        assert marker.encode() in response.content and author_id.encode() in response.content
        assert get_preview(patient, owner.session.session_key, job.pk, actor=patient.account).snapshot
        return
    record.refresh_from_db()
    if change == 'revision':
        assert record.current_data['raw_value'] == '6.70'
    else:
        assert record.created_by_id is None and record.updated_by_id is None
    # The invalidation must already have committed before any subsequent read.
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {}
    with pytest.raises(ExportUnavailable):
        get_preview(patient, owner.session.session_key, job.pk, actor=patient.account)
    assert response.status_code == 409
    assert marker.encode() not in response.content and author_id.encode() not in response.content
    assert b'name="format"' not in response.content


@pytest.mark.parametrize('change', ['revision', 'author_purge'])
def test_source_change_after_real_artifact_build_prevents_publication(django_user_model, monkeypatch, change):
    from apps.exports import services

    owner, patient, _, actor, _ = family(django_user_model, 'glucose-publication-' + change)
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    assert owner.get('/records/').status_code == 200
    job = create_preview(patient, owner.session.session_key, selection(record), actor=patient.account)
    request_generation(patient, owner.session.session_key, job.pk, {'format': 'json'},
                       actor=patient.account, dispatch=lambda _: None)
    original = services.build_artifact
    built = []

    def build_then_change(*args, **kwargs):
        artifact = original(*args, **kwargs)
        built.append(artifact.byte_size)
        if change == 'author_purge':
            purge(actor)
        else:
            revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='6.70'))
        return artifact

    monkeypatch.setattr(services, 'build_artifact', build_then_change)
    store = InMemoryObjectStore()
    generate_export(job.pk, store)
    assert len(built) == 1 and built[0] > 0
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and not job.object_key
    assert not store.objects
    with pytest.raises(ExportUnavailable):
        get_preview(patient, owner.session.session_key, job.pk, actor=patient.account)


@pytest.mark.parametrize('change', ['revision', 'author_purge'])
def test_source_change_after_actual_share_render_discards_the_old_body(django_user_model, monkeypatch, change):
    from apps.patients import share_views

    _, patient, _, actor, _ = family(django_user_model, 'glucose-render-' + change)
    record = create_record(patient, actor, payload(notes='SELECTED VALUE MUST DISAPPEAR'), creation_key=uuid4()).record
    created = create_share(patient, patient.account, selection(record))
    reader, own = _patient(django_user_model, 'glucose-render-reader-' + change)
    assert reader.get('/shared/open/').status_code == 200
    exchange_share_token(created.token, own.account, reader.session.session_key)
    render = share_views.render
    rendered = []

    def render_then_change(request, template, context=None, *args, **kwargs):
        response = render(request, template, context, *args, **kwargs)
        if template == 'patients/shared_detail.html':
            assert b'SELECTED VALUE MUST DISAPPEAR' in response.content
            rendered.append(True)
            if change == 'author_purge':
                purge(actor)
            else:
                revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='6.90'))
        return response

    monkeypatch.setattr(share_views, 'render', render_then_change)
    response = reader.get(f'/shared/{created.share.pk}/')
    assert rendered == [True]
    assert response.status_code == 410 and b'SELECTED VALUE MUST DISAPPEAR' not in response.content
    created.share.refresh_from_db()
    assert created.share.invalidated_at is not None and created.share.snapshot == {}


def test_real_multichunk_glucose_download_stops_after_correction_and_records_denial_once(django_user_model, monkeypatch):
    from apps.operations.audit import _hash
    from apps.operations.models import AuditEvent

    _, patient, client, actor, _ = family(django_user_model, 'glucose-stream-selected-change')
    records = [create_record(patient, actor, payload(notes='合成备注' + '记' * 480), creation_key=uuid4()).record
               for _ in range(130)]
    scope = {**selection(records[0]), 'glucose_record_ids': [str(record.pk) for record in records]}
    job = create_preview(patient, client.session.session_key, scope, actor=actor)
    request_generation(patient, client.session.session_key, job.pk, {'format': 'json'},
                       actor=actor, dispatch=lambda _: None)
    store = InMemoryObjectStore()
    generate_export(job.pk, store)
    monkeypatch.setattr('apps.exports.views.get_object_store', lambda: store)
    response = client.get(f'/visit/{job.pk}/download/')
    assert response.status_code == 200 and int(response['Content-Length']) > 256 * 1024
    stream = iter(response.streaming_content)
    assert len(next(stream)) == 256 * 1024
    revise_record(patient, actor, records[0].pk, action='CORRECT', expected_revision=0, changes=payload(value='6.30'))
    assert b''.join(stream) == b''
    response.close()
    response.close()
    events = list(AuditEvent.objects.filter(action='export_downloaded', target_hash=_hash('target', job.pk)))
    assert sorted(event.result for event in events) == ['denied', 'scheduled']
    assert len({event.request_id for event in events}) == 1
    assert all(event.actor_hash == _hash('actor', actor.pk) for event in events)
