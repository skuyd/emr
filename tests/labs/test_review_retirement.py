from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.labs.models import ReviewTask
from apps.labs.review import create_review_task
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('route,args,method', [
    ('labs:reviews', (), 'get'),
    ('labs:review_task', ('task',), 'get'),
    ('labs:review_task', ('task',), 'post'),
    ('labs:review_source', ('task', 'raw_value'), 'get'),
    ('labs:review_source_image', ('task', 'raw_value'), 'get'),
    ('labs:create_task', ('row',), 'post'),
])
def test_old_review_endpoints_are_retired_without_task_content(django_user_model, route, args, method):
    client, patient = _patient(django_user_model, 'retired-' + route + method)
    _, row, _ = report(patient)
    task = ReviewTask.objects.create(observation=row, granted_by=patient.account,
                                     expires_at=timezone.now() + timedelta(days=7))
    path = reverse(route, args=[str(task.pk if item == 'task' else row.pk) if item in {'task', 'row'}
                                else item for item in args])
    response = getattr(client, method)(path)
    assert response.status_code == 410
    assert row.raw_name not in response.content.decode()
    assert ReviewTask.objects.get(pk=task.pk).status == task.status


def test_service_cannot_create_new_review_task(django_user_model):
    _, patient = _patient(django_user_model, 'retired-service')
    _, row, _ = report(patient)
    with pytest.raises(PermissionDenied):
        create_review_task(patient.account, row.pk)
    assert not ReviewTask.objects.exists()


def test_completed_historical_review_result_remains_visible_after_retirement(django_user_model):
    from apps.labs.revisions import append_revision, effective_observation

    client, patient = _patient(django_user_model, 'retired-preserves-result')
    _, row, _ = report(patient)
    append_revision(patient.account, row, action='CORRECT', changes={'raw_value': '7'},
                    expected_revision=0, origin='REVIEW')
    row.refresh_from_db()

    assert effective_observation(row).raw_value == '7'
    response = client.get('/labs/reports/review/', {'patient': str(patient.pk)})
    assert response.status_code == 200
    assert 'value="7"' in response.content.decode()
    assert '修订历史' in response.content.decode()
