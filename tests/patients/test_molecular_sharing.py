"""Real limited shares use the same molecular unit and remain live-authorized."""
from urllib.parse import parse_qs, urlsplit
from datetime import timedelta
import json

import pytest
from django.utils import timezone

from apps.patients.models import PatientShare, PatientMembership
from apps.patients.sharing import create_share
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_shares import ShareLink, exchange
from tests.exports.test_molecular_exports import ready_graph, selection
from tests.facts.pathology_factories import review

pytestmark = pytest.mark.django_db


def test_actual_http_share_exchange_preserves_only_selected_molecular_value(django_user_model):
    owner, patient, document, report, fields = ready_graph(django_user_model, 'molecular-http-owner')
    response = owner.post(f'/patients/{patient.pk}/shares/', {
        'document_ids': [str(document.pk)], 'clinical_field_ids': [str(fields['metric'].pk)],
        'sections': ['imaging'], 'expires_in_hours': 24})
    assert response.status_code == 201
    parser = ShareLink(); parser.feed(response.content.decode())
    token = parse_qs(urlsplit(parser.value).fragment)['token'][0]
    viewer, _ = _patient(django_user_model, 'molecular-http-viewer')
    share_id = exchange(viewer, token)
    page = viewer.get(f'/shared/{share_id}/')
    assert page.status_code == 200
    text = page.content.decode()
    assert '01.20' in text and 'c.12+1G&gt;A' in text and 'NM_SYN.2' in text
    for private in ('标本甲', '检测甲', 'variant:a', str(fields['identity'].pk)):
        assert private not in text
    share = PatientShare.objects.get(pk=share_id)
    assert [r['id'] for r in share.snapshot['clinical_fields']] == [str(fields['metric'].pk)]
    assert 'molecular_validation_context' not in share.snapshot
    assert 'manual_source' not in json.dumps(share.snapshot)
    assert viewer.get(f'/shared/{share_id}/documents/{document.pk}/').status_code == 404
    assert viewer.get(f'/shared/{share_id}/documents/{document.pk}/original/').status_code == 404
    assert viewer.get(f'/facts/{fields["metric"].pk}/').status_code == 404
    assert viewer.get(f'/facts/reports/{report.pk}/').status_code == 404


@pytest.mark.parametrize('change', ['identity_exclude', 'selected_revoke', 'expire', 'scope_fields', 'scope_policy', 'creator_inactive', 'restore_after_revoke'])
def test_actual_share_rechecks_sources_scope_and_author_then_scrubs(django_user_model, change):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-change-' + change)
    result = create_share(patient, patient.account, selection(document, fields['metric']))
    viewer, _ = _patient(django_user_model, 'molecular-reader-' + change)
    share_id = exchange(viewer, result.token)
    assert viewer.get(f'/shared/{share_id}/').status_code == 200
    if change == 'identity_exclude': review(patient, fields['identity'], 'EXCLUDE')
    elif change in {'selected_revoke', 'restore_after_revoke'}: review(patient, fields['metric'], 'REVOKE')
    elif change == 'expire': PatientShare.objects.filter(pk=share_id).update(expires_at=timezone.now() - timedelta(seconds=1))
    elif change.startswith('scope_'):
        scope = result.share.scope.copy()
        scope['clinical_field_ids' if change == 'scope_fields' else 'molecular_semantic_unit_policy'] = [] if change == 'scope_fields' else 'NONE'
        PatientShare.objects.filter(pk=share_id).update(scope=scope)
    else:
        patient.account.is_active = False; patient.account.save(update_fields=['is_active'])
    response = viewer.get(f'/shared/{share_id}/')
    assert response.status_code in {404, 410}
    assert 'NM_SYN.2' not in response.content.decode()
    result.share.refresh_from_db()
    assert result.share.snapshot == {} and result.share.invalidated_at is not None
    if change == 'restore_after_revoke':
        review(patient, fields['metric'])
        assert viewer.get(f'/shared/{share_id}/').status_code == 410


@pytest.mark.parametrize('role', ['VIEWER', 'EDITOR'])
def test_non_managing_family_member_cannot_share_selected_molecular_unit(django_user_model, role):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-share-role-' + role)
    member, own = _patient(django_user_model, 'molecular-role-member-' + role)
    PatientMembership.objects.create(patient=patient, account=own.account, role=role)
    response = member.post(f'/patients/{patient.pk}/shares/', {'document_ids': [str(document.pk)],
        'clinical_field_ids': [str(fields['metric'].pk)], 'sections': ['imaging']})
    assert response.status_code == 403 and not PatientShare.objects.filter(patient=patient).exists()


def test_two_shares_have_fresh_aliases_and_recipient_session_is_required(django_user_model):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-share-fresh')
    first, second = [create_share(patient, patient.account, selection(document, fields['metric'])) for _ in range(2)]
    units = [r.share.snapshot['clinical_fields'][0]['content']['molecular_semantic_unit'] for r in (first, second)]
    assert units[0]['assay_scope']['token'] != units[1]['assay_scope']['token']
    reader, _ = _patient(django_user_model, 'molecular-session-reader')
    share_id = exchange(reader, first.token)
    assert reader.get(f'/shared/{share_id}/').status_code == 200
    reader.logout()
    assert reader.get(f'/shared/{share_id}/').status_code in {302, 401, 404}
