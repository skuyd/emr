from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


def imaging_observation(django_user_model, *, patient=None, day="2026-08-01", site="左肺上叶",
                        size="12", name="lesion-relations", confirmed=True, unit="mm", suv=None,
                        impression="建议结合临床及随访。"):
    if patient is None:
        _, patient = _patient(django_user_model, name)
    document, version = parsed_facts(patient, [
        "合成医院 CT诊断报告书",
        f"检查日期：{day} 检查项目：胸部CT平扫",
        f"影像表现：{site}见结节，长径{size}{unit}" + (f"，SUVmax{suv}" if suv else "") + "。",
        "诊断意见：" + impression,
    ], document_type="IMAGING")
    extract_clinical_version(version)
    report = document.clinical_reports.get()
    if confirmed:
        for field in report.fields.all():
            current = effective_fact(field)
            revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                        expected_source=current["current_source_token"], checked_original=True)
    return patient, document, report
