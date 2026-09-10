import io

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from pypdf import PdfReader

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.exports.errors import ExportUnavailable
from apps.exports.pdf import render_pdf
from apps.exports.services import create_preview, get_preview
from apps.lesions.services import rename_lesion
from apps.patients.models import PatientMembership
from apps.patients.sharing import create_share
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_lesion_portable_formats import prepared
from tests.exports.test_lesion_selected_material import ids
from tests.patients.test_family_shares import exchange


pytestmark = pytest.mark.django_db


def authenticated(patient):
    client = Client()
    client.force_login(patient.account)
    assert client.get('/me/').status_code == 200
    return client


def test_export_foreign_keys_bind_hidden_observation_dependencies_and_detect_removed_binding(django_user_model):
    patient, reports, _, scope = prepared(django_user_model, 'lesion-export-fk')
    client = authenticated(patient)
    scope['document_ids'] = [str(reports[0].document_id)]
    scope['clinical_field_ids'] = ids(reports[0], 'lesion.site', 'lesion.dimensions')
    job = create_preview(patient, client.session.session_key, scope, actor=patient.account)
    assert job.lesion_sources.filter(lesion__isnull=False).count() == 1
    assert job.lesion_sources.filter(observation__isnull=False).count() == 2
    assert job.source_bindings.filter(document_id=reports[1].document_id).exists()
    hidden = job.lesion_sources.get(observation__report=reports[1])
    original = hidden.original_id
    # Emulate actual FK SET_NULL; the original recorded identity remains.
    type(hidden).objects.filter(pk=hidden.pk).update(observation=None)
    hidden.refresh_from_db()
    assert hidden.original_id == original
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=patient.account)
    job.refresh_from_db()
    assert job.snapshot == {}


def test_real_shared_page_and_pdf_show_selected_group_without_normal_source_urls(django_user_model):
    patient, reports, snapshot, scope = prepared(django_user_model, 'lesion-share-visible')
    made = create_share(patient, patient.account, scope)
    assert made.share.lesion_sources.filter(observation__isnull=False).count() == 2
    viewer, _ = _patient(django_user_model, 'lesion-output-reader')
    identity = exchange(viewer, made.token)
    response = viewer.get(f'/shared/{identity}/')
    html = response.content.decode()
    assert response.status_code == 200 and '人工确认的观察分组' in html
    assert '=选定观察 &lt;A&gt;' in html and '=选定观察 <A>' not in html
    assert '算术差值' in html and '2.5' in html
    assert '/facts/' not in html and '/lesions/' not in html
    assert viewer.get(f'/shared/{identity}/documents/{reports[0].document_id}/original/').status_code == 404
    pdf_text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(render_pdf(snapshot))).pages)
    assert '人工确认的观察分组' in pdf_text and '1.25' in pdf_text and '12.5' in pdf_text and '差值' in pdf_text
    lesion = patient.lesions.get()
    rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                  expected_revision=lesion.revision_number, name='新名称')
    assert viewer.get(f'/shared/{identity}/').status_code == 410


def test_actual_intermediate_name_author_purge_invalidates_later_owner_named_output(django_user_model):
    patient, _, _, scope = prepared(django_user_model, 'lesion-output-author')
    editor_client, editor_patient = _patient(django_user_model, 'lesion-output-old-editor')
    editor = editor_patient.account
    PatientMembership.objects.create(patient=patient, account=editor, role='EDITOR')
    lesion = patient.lesions.get()
    rename_lesion(patient, actor=editor, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='中间作者名称')
    lesion.refresh_from_db()
    rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='现作者名称')
    owner = authenticated(patient)
    job = create_preview(patient, owner.session.session_key, scope, actor=patient.account)
    deletion = request_account_deletion(editor.pk, document_dispatch=lambda *_: None, account_dispatch=lambda *_: None)
    assert purge_account_deletion(deletion.pk).outcome == AccountDeletionOutcome.PURGED
    assert not editor.__class__.objects.filter(pk=editor.pk).exists()
    assert lesion.revisions.filter(operation__author__isnull=True).exists()
    with pytest.raises(ExportUnavailable):
        get_preview(patient, owner.session.session_key, job.pk, actor=patient.account)


def test_binding_rejects_another_patients_actual_graph_row(django_user_model):
    from apps.lesions.models import LesionExportSource

    patient, _, _, scope = prepared(django_user_model, 'lesion-output-parent')
    other, _, _, _ = prepared(django_user_model, 'lesion-output-foreign')
    owner = authenticated(patient)
    job = create_preview(patient, owner.session.session_key, scope, actor=patient.account)
    record = other.lesions.get()
    row = LesionExportSource(job=job, lesion=record, kind='LESION', original_id=record.pk)
    with pytest.raises(ValidationError):
        row.full_clean()
