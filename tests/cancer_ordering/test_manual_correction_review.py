from copy import deepcopy

import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from tests.cancer_ordering.test_services import _collect, _revise
from tests.documents.test_detail_viewer import _patient


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('original', ['出院诊断：未见肺癌。', '出院诊断：胰腺癌。'])
def test_revoked_manual_correction_needs_new_review_through_exclusion_and_undo(django_user_model, original):
    _, patient = _patient(django_user_model, 'cancer-manual-correction-' + original)
    _collect(patient, [original])
    candidate = CancerCandidate.objects.get(patient=patient)
    automatic = deepcopy(candidate.original_data)
    row = lambda: candidate_rows(patient)[0]
    _revise(patient, row(), 'CORRECT', checked_original=True, reason='对照合成原件更正', changes={
        'label': '肺癌', 'profile': 'LUNG', 'assertion': 'AFFIRMED', 'subject': 'CURRENT_PRIMARY'})
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    _revise(patient, row(), 'REVOKE')
    assert row()['status'] == 'PENDING' and row()['manual_correction']
    assert not row()['eligible_for_auto'] and resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, row(), 'EXCLUDE')
    _revise(patient, row(), 'UNDO')
    assert row()['status'] == 'PENDING' and resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, row(), 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    candidate.refresh_from_db()
    assert candidate.original_data == automatic and row()['manual_correction']


def test_unmodified_original_can_still_use_its_own_ocr_after_confirmation_revoke(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-unmodified-ocr-revoke')
    _collect(patient)
    row = lambda: candidate_rows(patient)[0]
    _revise(patient, row(), 'CONFIRM', checked_original=True)
    _revise(patient, row(), 'REVOKE')
    assert not row()['manual_correction'] and row()['status'] == 'PENDING'
    assert row()['eligible_for_auto'] and resolve_ordering(patient)['profile'] == 'LUNG'
