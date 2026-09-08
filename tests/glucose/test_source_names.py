import pytest

from apps.glucose.sources import GlucoseSourceUnavailable, preview_lab
from tests.glucose.factories import lab_source


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('raw_name,slot', [
    ('△*葡萄糖', 'UNSPECIFIED'),
    ('☆ 血糖', 'UNSPECIFIED'),
    ('GLU 葡萄糖', 'UNSPECIFIED'),
    ('葡萄糖 GLU', 'UNSPECIFIED'),
    ('6 ★GLU 葡萄糖', 'UNSPECIFIED'),
    ('12. 葡萄糖（ＧＬＵ）', 'UNSPECIFIED'),
    ('３、★ 空腹血糖', 'FASTING'),
    ('GLU 空腹葡萄糖', 'FASTING'),
])
def test_printed_markers_and_matching_abbreviation_preserve_original_source_name(
        django_user_model, raw_name, slot):
    _, patient, _, _, observation = lab_source(django_user_model, raw_name=raw_name)
    data = preview_lab(patient, patient.account, observation.pk)['data']
    source = data['source']['field_sources']['raw_name']
    assert data['raw_value'] == '8.20' and data['time_slot'] == slot
    assert source['effective_value'] == raw_name
    assert source['original_text'] == observation.evidence.source_text
    observation.refresh_from_db()
    assert observation.raw_name == raw_name


@pytest.mark.parametrize('raw_name', [
    'GLU 尿葡萄糖', '6 ★葡萄糖注射液', 'GLU 糖化血红蛋白',
    '非空腹血糖', '6葡萄糖', '13C葡萄糖', 'GLU 葡萄糖 NEUT',
    'GLU GLU 葡萄糖', '葡萄糖 / 乳酸', '取消葡萄糖',
])
def test_alias_recognition_does_not_discard_other_meaningful_name_content(django_user_model, raw_name):
    _, patient, _, _, observation = lab_source(django_user_model, raw_name=raw_name)
    with pytest.raises(GlucoseSourceUnavailable):
        preview_lab(patient, patient.account, observation.pk)


@pytest.mark.parametrize('specimen,code', [
    ('', 'LAB_FASTING_GLUCOSE'), ('URINE', 'LAB_FASTING_GLUCOSE'), ('BLOOD', 'CANDIDATE_UNRESOLVED'),
])
def test_matching_decorated_name_does_not_replace_unknown_or_conflicting_source_fields(
        django_user_model, specimen, code):
    _, patient, _, _, observation = lab_source(django_user_model, raw_name='6 ★GLU 葡萄糖', specimen=specimen)
    observation.standard_code = code
    observation.save(update_fields=['standard_code'])
    with pytest.raises(GlucoseSourceUnavailable):
        preview_lab(patient, patient.account, observation.pk)
