"""HTTP reads remain current across real account deletion and lab revisions."""
from copy import deepcopy
from uuid import uuid4

import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.accounts.models import AccountDeletionJob
from apps.glucose import views
from apps.glucose.services import create_record, import_lab_record, revise_record
from apps.glucose.sources import preview_lab, source_current
from apps.labs.revisions import effective_observation, revise_observation
from tests.glucose.factories import lab_source
from tests.glucose.test_forms import values
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize('only_historical_author', [False, True])
def test_detail_discards_current_or_historical_author_purged_during_render(
        django_user_model, monkeypatch, only_historical_author):
    owner, patient, _, actor, _ = family(django_user_model, 'glucose-purged-author-' + str(only_historical_author))
    creator = patient.account if only_historical_author else actor
    record = create_record(patient, creator, values(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=values(value='8.1'))
    if only_historical_author:
        revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=1, changes=values(value='8.2'))
    identity = str(actor.pk)
    original_render = views.render

    def purge_after_render(*args, **kwargs):
        response = original_render(*args, **kwargs)
        assert identity in response.content.decode()
        request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        job = AccountDeletionJob.objects.get(account_id=actor.pk)
        assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
        return response

    with monkeypatch.context() as patch:
        patch.setattr(views, 'render', purge_after_render)
        response = owner.get(f'/glucose/{record.pk}/')
    record.refresh_from_db()
    assert not record.revisions.filter(author_id=identity).exists()
    if not only_historical_author:
        assert record.created_by_id is None and record.updated_by_id is None
    assert response.status_code == 409 and identity not in response.content.decode()
    fresh = owner.get(f'/glucose/{record.pk}/')
    assert fresh.status_code == 200 and '已注销账号' in fresh.content.decode()
    assert identity not in fresh.content.decode() and record.current_data['raw_value'] in fresh.content.decode()


@pytest.mark.parametrize('page_name', ['index', 'detail', 'import', 'recheck', 'after-render'])
def test_upstream_lab_value_outside_glucose_limit_stays_stale_without_http_error(
        django_user_model, monkeypatch, page_name):
    client, patient, _, _, observation = lab_source(django_user_model, marker='glucose-wide-source-' + page_name)
    candidate = preview_lab(patient, patient.account, observation.pk)
    record = import_lab_record(patient, patient.account, observation.pk,
        expected_source=candidate['source_fingerprint'], checked_original=True, creation_key=uuid4(),
        confirm_timezone=True, timezone_name='Asia/Shanghai').record
    original_data, original_fingerprint = deepcopy(record.current_data), record.source_fingerprint
    assert client.get('/glucose/').context['charts'][0]['points_count'] == 1

    def change_source():
        # This is valid through the real 256-character upstream revision API.
        revise_observation(patient.account, observation.pk, action='CORRECT',
            changes={'raw_value': '1' * 161}, expected_revision=0)

    if page_name == 'after-render':
        original_render = views.render

        def change_after_render(*args, **kwargs):
            response = original_render(*args, **kwargs)
            change_source()
            return response

        monkeypatch.setattr(views, 'render', change_after_render)
    else:
        change_source()
    url = {'index': '/glucose/', 'detail': f'/glucose/{record.pk}/',
        'import': f'/glucose/import/labs/{observation.pk}/',
        'recheck': f'/glucose/{record.pk}/recheck/', 'after-render': '/glucose/'}[page_name]
    response = client.get(url, {'patient': str(patient.pk)})
    assert response.status_code == (200 if page_name in ('index', 'detail') else 409)
    if page_name in ('index', 'detail'):
        assert '来源已变化' in response.content.decode()
    if page_name == 'index':
        assert response.context['charts'] == [] and response.context['heatmaps'] == []
    if page_name == 'after-render':
        assert original_data['raw_value'] not in response.content.decode()
    record.refresh_from_db()
    observation.refresh_from_db()
    assert not source_current(record)
    assert record.revision_number == 0 and record.current_data == original_data
    assert record.source_fingerprint == original_fingerprint
    assert effective_observation(observation).raw_value == '1' * 161
