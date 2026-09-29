import uuid
from datetime import date

import pytest

from apps.labs.models import ReportAssociation, ReportAssociationEvent
from apps.labs.report_identity import extract_report_units
from apps.labs.report_workspace import report_workspace, submit_report_workspace
from apps.labs.revisions import RevisionConflict
from apps.labs.reports import persist_report_units, report_relations
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_identity import page
from tests.labs.test_report_relations import report
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def _workspace_report(patient, unit):
    return next(item for item in report_workspace(patient)['reports'] if unit in item['units'])


def _confirm(patient, item):
    return submit_report_workspace(patient, patient.account, item['key'], item['token'],
                                   uuid.uuid4(), {}, confirm=True)


def test_document_with_two_report_regions_offers_two_specific_details(django_user_model):
    client, patient = _patient(django_user_model, 'organize-document-regions')
    document, row = _observation(patient, date(2026, 9, 17), '5')
    identities = extract_report_units((page('合成医院', '检验报告', '报告号：A1',
        '采样时间：2026-09-17 08:30', '白细胞 5', '合成医院', '检验报告', '报告号：B2',
        '采样时间：2026-09-17 09:30', '白细胞 6'),))
    units = persist_report_units(row.parsing_version, identities)

    response = client.get(f'/records/{document.pk}/')

    assert response.status_code == 200
    content = response.content.decode()
    for unit in units:
        assert f'/labs/reports/{unit.pk}/' in content
        assert f'/labs/reports/{unit.pk}/organize/' in content
    assert 'A1' in content and 'B2' in content
    assert 'return_to=' in content


def test_organization_shows_all_current_sources_including_zero_results(django_user_model):
    client, patient = _patient(django_user_model, 'organize-zero-result-view')
    first_doc, _, first = report(patient)
    second_doc, _, second = report(patient)
    second.observations.all().delete()

    response = client.get(f'/labs/reports/{first.pk}/organize/')

    assert response.status_code == 200
    sources = response.context['current_sources']
    assert {card['unit'].pk for card in sources} == {first.pk, second.pk}
    content = response.content.decode()
    assert '整理报告' in content
    assert '保存原件核对与更正' not in content
    assert '确认本报告' not in content
    assert '批量确认' not in content
    assert f'/records/{first_doc.pk}/' in content
    assert f'/records/{first_doc.pk}/pages/1/image/' in content
    assert f'/records/{second_doc.pk}/pages/1/image/' in content
    assert '未识别' not in content or '报告号' in content


def test_organization_selects_candidate_and_saves_relation_in_place(django_user_model):
    client, patient = _patient(django_user_model, 'organize-candidate-view')
    _, _, first = report(patient, number='')
    _, _, second = report(patient, number='')
    path = f'/labs/reports/{first.pk}/organize/'

    preview = client.get(path, {'target': str(second.pk)})
    assert preview.status_code == 200
    assert preview.context['selected_right']['unit'].pk == second.pk
    assert '整理后报告范围' in preview.content.decode()
    response = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(second.pk), 'action': 'SAME', 'rationale': '对照原图确认两页同属一份报告',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})

    assert response.status_code == 302
    assert '/organize/' in response['Location']
    relation, = report_relations(patient)
    assert relation.state == 'SAME'
    refreshed = client.get(response['Location'])
    assert {card['unit'].pk for card in refreshed.context['current_sources']} == {first.pk, second.pk}
    assert '对照原图确认两页同属一份报告' in refreshed.content.decode()


def test_organization_keeps_stale_input_and_shows_error_by_selected_pair(django_user_model):
    client, patient = _patient(django_user_model, 'organize-stale-view')
    _, _, first = report(patient, number='')
    _, _, second = report(patient, number='')
    path = f'/labs/reports/{first.pk}/organize/'
    preview = client.get(path, {'target': str(second.pk)})
    old_token = preview.context['organization_token']
    valid = {'patient_id': str(patient.pk), 'left_id': str(first.pk), 'right_id': str(second.pk),
             'action': 'SAME', 'rationale': '第一次核对', 'expected_context': old_token,
             'operation_id': str(uuid.uuid4())}
    assert client.post(path, valid).status_code == 302

    stale = client.post(path, {**valid, 'rationale': '过期页面填写的依据', 'operation_id': str(uuid.uuid4())})

    assert stale.status_code == 409
    assert '过期页面填写的依据' in stale.content.decode()
    assert '报告来源或关系已变化' in stale.content.decode()
    assert stale.context['selected_right']['unit'].pk == second.pk
    assert ReportAssociationEvent.objects.filter(action='SAME').count() == 1


def test_stale_group_change_keeps_selected_sources_and_rationale(django_user_model):
    from apps.labs.reports import decide_relation

    client, patient = _patient(django_user_model, 'organize-stale-group-view')
    _, _, current = report(patient)
    _, _, member = report(patient)
    _, _, candidate = report(patient, number='B200')
    relation, = report_relations(patient)
    path = f'/labs/reports/{current.pk}/organize/'
    token = client.get(path).context['organization_token']
    decide_relation(patient, patient.account, relation.pk, 'UNDO',
                    expected_revision=relation.revision_number, rationale='重新核对后撤销', operation_id='split-before-post')

    response = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(member.pk),
        'right_id': str(candidate.pk), 'action': 'SAME', 'rationale': '原页面填写的判断依据',
        'expected_context': token, 'operation_id': str(uuid.uuid4())})

    assert response.status_code == 409
    assert response.context['selected_left']['unit'].pk == member.pk
    assert response.context['selected_right']['unit'].pk == candidate.pk
    assert '原页面填写的判断依据' in response.content.decode()
    assert '所选来源已不属于当前报告' in response.content.decode() or '报告来源或关系已变化' in response.content.decode()


def test_old_relation_address_keeps_context_but_cannot_write(django_user_model):
    client, patient = _patient(django_user_model, 'organize-legacy-route')
    first_doc, _, first = report(patient)
    report(patient)
    relation, = report_relations(patient)
    old = f'/labs/report-relations/{relation.pk}/'

    response = client.get(old, {'patient': str(patient.pk), 'document': str(first_doc.pk)})
    assert response.status_code == 302
    assert '/organize/' in response['Location']
    assert f'patient={patient.pk}' in response['Location']
    assert f'document={first_doc.pk}' in response['Location']
    before = ReportAssociationEvent.objects.count()
    blocked = client.post(old, {'patient_id': str(patient.pk), 'action': 'UNDO',
        'expected_revision': relation.revision_number, 'rationale': '旧表单', 'operation_id': str(uuid.uuid4())})
    assert blocked.status_code == 410
    assert ReportAssociationEvent.objects.count() == before
    assert ReportAssociation.objects.get(pk=relation.pk).state == 'AUTO'


def test_old_report_list_preserves_patient_and_selected_source(django_user_model):
    client, patient = _patient(django_user_model, 'organize-legacy-list')
    _, _, unit = report(patient)

    selected = client.get('/labs/reports/', {'patient': str(patient.pk), 'unit': str(unit.pk)})
    assert selected.status_code == 302
    assert selected['Location'].startswith(f'/labs/reports/{unit.pk}/organize/')
    assert f'patient={patient.pk}' in selected['Location']
    unselected = client.get('/labs/reports/', {'patient': str(patient.pk)})
    assert unselected.status_code == 302
    assert unselected['Location'] == f'/records/?patient={patient.pk}'


def test_organization_checks_read_scope_and_write_permission(django_user_model):
    from apps.patients.models import PatientMembership

    client, patient = _patient(django_user_model, 'organize-access-owner')
    foreign_client, foreign_patient = _patient(django_user_model, 'organize-access-foreign')
    _, _, first = report(patient, number='')
    _, _, second = report(patient, number='')
    _, _, foreign = report(foreign_patient, number='')
    path = f'/labs/reports/{first.pk}/organize/'
    assert foreign_client.get(path).status_code == 404
    assert client.get(path, {'target': str(foreign.pk)}).status_code == 404

    viewer, member = _patient(django_user_model, 'organize-access-viewer')
    PatientMembership.objects.create(patient=patient, account=member.account, role='VIEWER')
    shown = viewer.get(path, {'patient': str(patient.pk), 'target': str(second.pk)})
    assert shown.status_code == 200
    assert '确认同一报告' not in shown.content.decode()
    assert viewer.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(second.pk), 'action': 'SAME', 'rationale': '越权',
        'expected_context': shown.context['organization_token'], 'operation_id': str(uuid.uuid4())}).status_code == 403
    assert client.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(foreign.pk), 'action': 'SAME', 'rationale': '伪造来源',
        'expected_context': client.get(path).context['organization_token'],
        'operation_id': str(uuid.uuid4())}).status_code == 403
    assert not ReportAssociation.objects.exists()


def test_report_field_correction_can_precede_source_organization(django_user_model):
    client, patient = _patient(django_user_model, 'organize-after-field-correction')
    _, _, first = report(patient, number='')
    _, _, second = report(patient, number='B200')
    current = _workspace_report(patient, second)
    submit_report_workspace(patient, patient.account, current['key'], current['token'], uuid.uuid4(),
        {'reports': [{'unit_id': str(second.pk), 'expected_revision': 0,
                      'changes': {'report_number': 'A100'}}]}, confirm=False)

    path = f'/labs/reports/{first.pk}/organize/'
    preview = client.get(path, {'target': str(second.pk)})
    assert preview.status_code == 200
    assert preview.context['selected_right']['identity'].report_number == 'A100'
    saved = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(second.pk), 'action': 'SAME', 'rationale': '报告号已核对，原件是同一报告',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})
    assert saved.status_code == 302
    assert report_relations(patient)[0].state == 'SAME'
    assert second.revisions.count() == 1


def test_different_decision_leaves_candidate_independent_and_resolved(django_user_model):
    client, patient = _patient(django_user_model, 'organize-different-view')
    _, _, first = report(patient)
    _, _, second = report(patient, at='2026-09-17 09:30')
    relation, = report_relations(patient)
    path = f'/labs/reports/{first.pk}/organize/'
    preview = client.get(path, {'focus': str(relation.pk)})
    assert {card['unit'].pk for card in preview.context['related_candidates']} == {second.pk}

    saved = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(second.pk), 'action': 'DIFFERENT', 'rationale': '两份原件采样时间不同',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})

    assert saved.status_code == 302
    refreshed = client.get(path)
    assert refreshed.context['related_candidates'] == []
    assert {card['unit'].pk for card in refreshed.context['current_sources']} == {first.pk}
    assert report_relations(patient)[0].state == 'DIFFERENT'


def test_undo_preview_and_saved_page_show_remaining_relations(django_user_model):
    client, patient = _patient(django_user_model, 'organize-remaining-view')
    units = [report(patient)[2] for _ in range(3)]
    selected = next(item for item in report_relations(patient) if
                    {item.left_key, item.right_key} == {units[0].source_key, units[2].source_key})
    path = f'/labs/reports/{units[0].pk}/organize/'
    preview = client.get(path, {'focus': str(selected.pk)})
    assert len(preview.context['current_sources']) == 3
    effect, = preview.context['actions']
    assert effect['action'] == 'UNDO'
    assert len(effect['sources']) < 3
    assert effect['remaining_relations'] == 2

    saved = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(units[0].pk),
        'right_id': str(units[2].pk), 'action': 'UNDO', 'rationale': '第三份原图不属当前报告',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})
    assert saved.status_code == 302
    refreshed = client.get(saved['Location'])
    assert len(refreshed.context['current_sources']) < 3
    assert sorted(row['association'].state for row in refreshed.context['relation_rows']) == [
        'AUTO', 'AUTO', 'UNDONE']


def test_reassociate_keeps_originals_results_and_manual_revision(django_user_model):
    from apps.labs.report_reads import report_source_groups
    from apps.labs.revisions import revise_observation

    client, patient = _patient(django_user_model, 'organize-reassociate')
    wrong_doc, wrong_row, wrong = report(patient)
    original_doc, _, original = report(patient)
    correct_doc, _, correct = report(patient, number='B200')
    wrong_relation, = report_relations(patient)
    first_path = f'/labs/reports/{original.pk}/organize/'
    before = client.get(first_path, {'focus': str(wrong_relation.pk)})
    assert client.post(first_path, {'patient_id': str(patient.pk), 'left_id': str(original.pk),
        'right_id': str(wrong.pk), 'action': 'UNDO', 'rationale': '归属错误',
        'expected_context': before.context['organization_token'],
        'operation_id': str(uuid.uuid4())}).status_code == 302

    revise_observation(patient.account, wrong_row.pk, action='CORRECT', changes={'raw_value': '5.5'},
                       expected_revision=0)

    second_path = f'/labs/reports/{wrong.pk}/organize/'
    candidate = client.get(second_path, {'target': str(correct.pk)})
    assert client.post(second_path, {'patient_id': str(patient.pk), 'left_id': str(wrong.pk),
        'right_id': str(correct.pk), 'action': 'SAME', 'rationale': '原图报告号误识别，实属续页',
        'expected_context': candidate.context['organization_token'],
        'operation_id': str(uuid.uuid4())}).status_code == 302

    groups = report_source_groups(patient)
    assert groups[wrong.source_key] == groups[correct.source_key] == frozenset(
        {wrong.source_key, correct.source_key})
    assert groups[original.source_key] == frozenset({original.source_key})
    assert all(document.deleted_at is None and document.pages.count() == 1
               for document in (wrong_doc, original_doc, correct_doc))
    wrong_row.refresh_from_db()
    assert wrong_row.raw_value == '5'
    assert wrong_row.revisions.count() == 1
    assert ReportAssociationEvent.objects.filter(action='UNDO').count() == 1
    assert ReportAssociationEvent.objects.filter(action='SAME').count() == 1


def test_organizing_changes_shared_report_projection_and_rejects_old_confirmation(django_user_model):
    from apps.exports.content import build_snapshot
    from apps.labs.comparison import comparison_view
    from apps.labs.trends import trend_view

    client, patient = _patient(django_user_model, 'organize-shared-reads')
    _, _, first = report(patient)
    report(patient)
    relation, = report_relations(patient)
    old_confirmation = _workspace_report(patient, first)
    assert comparison_view(patient).report_count == 1
    assert build_snapshot(patient, {'mode': 'all'})['lab_results'][0]['report_count'] == 1
    path = f'/labs/reports/{first.pk}/organize/'
    preview = client.get(path, {'focus': str(relation.pk)})
    second = preview.context['selected_right']['unit']
    left = preview.context['selected_left']['unit']

    saved = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(left.pk),
        'right_id': str(second.pk), 'action': 'UNDO', 'rationale': '两份原图实为不同报告',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})

    assert saved.status_code == 302
    assert comparison_view(patient).report_count == 2
    assert build_snapshot(patient, {'mode': 'all'})['lab_results'][0]['report_count'] == 2
    trend = trend_view(patient, 'LAB_WBC', include_history=True)
    assert len(trend.daily_details) == 1
    assert len(trend.daily_details[0].sources) == 2
    with pytest.raises(RevisionConflict, match='来源或结果已变化'):
        _confirm(patient, old_confirmation)


def test_review_workspace_tracks_grouped_originals_and_results_after_organize_and_undo(django_user_model):
    from apps.labs.report_views import workspace_url
    from apps.labs.reports import organize_report_relation, report_organization_token

    client, patient = _patient(django_user_model, 'organize-detail-scope')
    first_doc, first_row, first = report(patient, number='A100', value='5')
    second_doc, second_row, second = report(patient, number='B200', value='6')
    token = report_organization_token(patient)
    organize_report_relation(patient, patient.account, first.pk, first.pk, second.pk, 'SAME',
        expected_context=token, rationale='对照原件确认同一报告', operation_id='detail-join')

    joined = client.get(workspace_url(patient, unit_id=first.pk))
    assert {row.pk for row in joined.context['current']['rows']} == {first_row.pk, second_row.pk}
    assert f'/records/{first_doc.pk}/pages/1/image/' in joined.content.decode()
    assert f'/records/{second_doc.pk}/pages/1/image/' in joined.content.decode()

    organize_report_relation(patient, patient.account, first.pk, first.pk, second.pk, 'UNDO',
        expected_context=report_organization_token(patient), rationale='原件实际不同', operation_id='detail-split')
    split = client.get(workspace_url(patient, unit_id=first.pk))
    assert {row.pk for row in split.context['current']['rows']} == {first_row.pk}
    assert f'/records/{second_doc.pk}/pages/1/image/' not in split.content.decode()


@pytest.mark.parametrize('change', ['deleted', 'inactive'])
def test_organization_stale_report_url_preserves_rationale(django_user_model, change):
    from django.utils import timezone

    client, patient = _patient(django_user_model, 'organize-stale-url-' + change)
    document, _, first = report(patient, number='A100')
    _, _, second = report(patient, number='B200')
    path = f'/labs/reports/{first.pk}/organize/'
    preview = client.get(path, {'target': str(second.pk)})
    if change == 'deleted':
        document.deleted_at = timezone.now()
        document.save(update_fields=['deleted_at'])
    else:
        version = first.parsing_version
        version.active = False
        version.save(update_fields=['active'])

    response = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(second.pk), 'action': 'SAME', 'rationale': '已填写的原件依据',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})
    assert response.status_code == 409
    assert '原件已删除或解析版本已变化' in response.content.decode()
    assert '已填写的原件依据' in response.content.decode()
    assert not ReportAssociationEvent.objects.filter(action='SAME').exists()


def test_conflicting_manual_join_remains_visibly_flagged(django_user_model):
    client, patient = _patient(django_user_model, 'organize-visible-conflict')
    _, _, first = report(patient)
    _, _, second = report(patient, at='2026-09-17 09:30')
    path = f'/labs/reports/{first.pk}/organize/'
    preview = client.get(path, {'target': str(second.pk)})
    saved = client.post(path, {'patient_id': str(patient.pk), 'left_id': str(first.pk),
        'right_id': str(second.pk), 'action': 'SAME', 'rationale': '仍按原件归为一份',
        'expected_context': preview.context['organization_token'], 'operation_id': str(uuid.uuid4())})
    assert saved.status_code == 302
    content = client.get(saved['Location']).content.decode()
    assert '报告归属存在冲突' in content


@pytest.mark.parametrize('action', ['SAME', 'UNDO'])
def test_previous_report_confirmation_does_not_cover_changed_source_scope(django_user_model, action):
    from apps.labs.reports import organize_report_relation, report_organization_token

    _, patient = _patient(django_user_model, 'organize-confirm-scope-' + action)
    _, _, first = report(patient, number='A100')
    _, _, second = report(patient, number='B200')
    if action == 'UNDO':
        organize_report_relation(patient, patient.account, first.pk, first.pk, second.pk, 'SAME',
            expected_context=report_organization_token(patient), rationale='同一报告', operation_id='before-confirm')
    before = report_workspace(patient)['reports']
    for item in before:
        _confirm(patient, item)
    assert all(item['confirmed'] for item in report_workspace(patient)['reports'])

    organize_report_relation(patient, patient.account, first.pk, first.pk, second.pk, action,
        expected_context=report_organization_token(patient), rationale='重新判断来源关系', operation_id='change-scope')
    changed = report_workspace(patient)['reports']
    assert changed and all(not item['confirmed'] for item in changed)
    with pytest.raises(RevisionConflict):
        _confirm(patient, before[0])
    for item in changed:
        _confirm(patient, item)
    assert all(item['confirmed'] for item in report_workspace(patient)['reports'])


def test_report_confirmation_scope_includes_zero_result_source(django_user_model):
    _, patient = _patient(django_user_model, 'organize-confirm-zero-source')
    report(patient)
    _, _, continuation = report(patient)
    continuation.observations.all().delete()
    current, = report_workspace(patient)['reports']
    assert len(current['units']) == 2
    _confirm(patient, current)
    assert report_workspace(patient)['current']['confirmed']
