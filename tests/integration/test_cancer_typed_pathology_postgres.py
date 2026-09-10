"""Typed unselected context changes at real committed response boundaries."""
import pytest

from apps.exports import services, views
from apps.patients import share_views
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_typed_pathology_outputs import setup
from tests.documents.test_detail_viewer import _patient
from tests.facts.pathology_factories import review
from tests.integration.test_cancer_narrative_output_postgres import (
    at_boundary, committed, committed_job, hidden, require_postgresql,
)
from tests.patients.test_family_shares import exchange

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize('kind', ['preview', 'share'])
@pytest.mark.parametrize('changed', [False, True])
def test_committed_anchor_change_discards_rendered_selected_candidate(request, django_user_model, monkeypatch, kind, changed):
    owner, patient, _, _, anchor, selection = setup(django_user_model)
    assert owner.get('/records/').status_code == 200
    if kind == 'preview':
        output = services.create_preview(patient, owner.session.session_key, selection, actor=patient.account)
        client, path, module = owner, f'/visit/{output.pk}/', views
    else:
        client, _ = _patient(django_user_model, 'typed-pg-viewer')
        created = create_share(patient, patient.account, selection)
        identity = exchange(client, created.token)
        output, path, module = created.share, f'/shared/{identity}/', share_views
    response = at_boundary(request, monkeypatch, module, 'render', lambda: client.get(path),
        lambda: review(patient, anchor, 'DEFER') if changed else None)
    assert response.status_code == ((409 if kind == 'preview' else 410) if changed else 200)
    if changed:
        assert '肺癌' not in response.content.decode()
        snapshot = committed(request, 'scrub_readback', lambda: type(output).objects.get(pk=output.pk).snapshot)
        assert snapshot == {}
    else:
        assert '肺癌' in response.content.decode()
    response.close()


@pytest.mark.parametrize('changed', [False, True])
def test_started_pdf_checks_committed_unselected_specimen_before_next_chunk(request, django_user_model, changed):
    owner, patient, _, _, anchor, selection = setup(django_user_model)
    assert owner.get('/records/').status_code == 200
    job = services.create_preview(patient, owner.session.session_key, selection, actor=patient.account)
    response = owner.get(f'/visit/{job.pk}/pdf/')
    assert response.status_code == 200
    response.block_size = 96
    chunks = iter(response.streaming_content)
    assert len(next(chunks)) == 96
    committed(request, 'typed_anchor_commit', lambda: review(patient, anchor, 'DEFER') if changed else None)
    remaining = list(chunks)
    if changed:
        assert remaining == [] and response._guarded_stream.denied
        hidden(committed_job(request, job))
    else:
        assert remaining and response._guarded_stream.exhausted
    response.close()
