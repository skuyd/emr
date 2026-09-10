"""Synthetic selected molecular, cancer, lesion and cloud meanings stay separate."""
from copy import deepcopy
import json
import zipfile

import pytest

from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import build_artifact, json_bytes, read_structured_data
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_lesion_output_compatibility import mixed_selected
from tests.cancer_ordering.test_services import _select
from tests.cloud_imaging.test_source_services import FIRST_URL, _decide
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.molecular_factories import add, variant_source
from tests.facts.pathology_factories import review
from tests.facts.test_molecular_contracts import variant, quantity
from tests.patients.test_family_shares import exchange

pytestmark = pytest.mark.django_db
ARRAYS = ("cancer_candidates", "indicator_ordering", "lesions", "lesion_observations",
          "lesion_measurements", "cloud_imaging_sources", "cloud_imaging_evidence")


def four_domains(user_model):
    from apps.cancer_ordering.readmodels import resolve_ordering
    from apps.facts.clinical_services import create_manual_report
    patient, reports, lesion, cloud, scope = mixed_selected(user_model)
    document, _ = _document(patient, page_count=1, status="PROCESSING_FAILED")
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk,
        spans=[{"page_number": 1}], title="合成分子检测报告", expected_lifecycle_revision=document.lifecycle_revision,
        expected_version_id=None, routing_kind="MOLECULAR")
    specimen = add(patient, report, "specimen.identity", "specimen:a", {"label":"标本甲", "raw":"标本甲"}, {})
    assay = add(patient, report, "assay.identity", "assay:a", {"label":"检测甲", "raw":"检测甲"}, {"SPECIMEN":specimen})
    targets = {"SPECIMEN": specimen, "ASSAY": assay}
    identity = add(patient, report, "variant.identity", "variant:a", variant(), targets)
    metric = add(patient, report, "variant.allele_fraction", "variant:a", quantity(), {**targets,"VARIANT":identity},
                 raw="标本甲；检测甲；" + variant_source() + "; 01.20 %")
    for field in (specimen, assay, identity, metric): review(patient, field)
    scope["document_ids"].append(str(document.pk))
    scope["clinical_field_ids"].append(str(metric.pk))
    scope["cancer_expected_fingerprint"] = resolve_ordering(patient)["fingerprint"]
    return patient, reports, lesion, cloud, scope, metric, document


def change_domain(patient, lesion, cloud, metric, domain):
    from apps.lesions.services import rename_lesion
    if domain == "molecular": review(patient, metric, "EXCLUDE")
    elif domain == "cancer": _select(patient, "MANUAL_PROFILE", profile="PANCREAS")
    elif domain == "lesion":
        rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                     expected_revision=lesion.revision_number, name="SYNTHETIC_CHANGED_LESION")
    elif domain == "cloud": _decide(patient, cloud, "EXCLUDE")
    else: raise AssertionError(domain)


def test_current_four_domain_zip_share_and_required_arrays(django_user_model):
    patient, _, _, cloud, scope, metric, _ = four_domains(django_user_model)
    snapshot = build_snapshot(patient, scope)
    data = read_structured_data(json_bytes(snapshot))
    assert data["schema_version"] == "1.8" and all(data[key] for key in ARRAYS)
    field = next(row for row in data["clinical_fields"] if row["id"] == str(metric.pk))
    assert field["content"]["value"]["values"] == ["01.20"]
    assert field["content"]["molecular_semantic_unit"]["variants"][0]["identity"]["transcripts"]["values"] == ["NM_SYN.2"]
    for key in ARRAYS:
        damaged = deepcopy(data); del damaged[key]
        with pytest.raises(ExportInputError): read_structured_data(json.dumps(damaged))
    with build_artifact(snapshot, {"format":"zip", "parts":["json","csv","pdf"]}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            assert read_structured_data(archive.read("records.json")) == data
            assert all("csv/"+key+".csv" in archive.namelist() for key in ARRAYS)
            assert not any(name.startswith("originals/") for name in archive.namelist())
    made = create_share(patient, patient.account, scope)
    assert all(made.share.snapshot[key] for key in ARRAYS if key != "cloud_imaging_evidence")
    assert made.share.snapshot["cloud_imaging_evidence"] == []
    assert FIRST_URL not in json.dumps(made.share.snapshot)
    assert made.share.cloud_sources.get().source_id == cloud.pk
    assert made.share.lesion_sources.exists()


def test_actual_16_17_compatibility_is_not_unpublished_molecular_16(django_user_model):
    patient, _, _, _, scope, metric, document = four_domains(django_user_model)
    previous = deepcopy(scope)
    previous["clinical_field_ids"].remove(str(metric.pk))
    data = json.loads(json_bytes(build_snapshot(patient, previous)))
    data["schema_version"] = "1.7"
    original = deepcopy(data)
    restored = read_structured_data(json.dumps(data))
    assert all(restored[key] == original[key] and restored[key] for key in ARRAYS)
    assert data == original
    previous.update(cancer_candidate_ids=[], include_indicator_ordering=False)
    data = json.loads(json_bytes(build_snapshot(patient, previous)))
    data["schema_version"] = "1.6"
    for key in ("cancer_candidates", "indicator_ordering"): assert data.pop(key) == []
    original = deepcopy(data)
    restored = read_structured_data(json.dumps(data))
    assert all(restored[key] == original[key] and restored[key] for key in ARRAYS[2:])
    assert restored["cancer_candidates"] == restored["indicator_ordering"] == [] and data == original
    molecular_only = {**scope,"document_ids":[str(document.pk)],"clinical_field_ids":[str(metric.pk)],
                      "cancer_candidate_ids":[],"include_indicator_ordering":False,"lesion_ids":[],"cloud_source_ids":[],"sections":["imaging"]}
    data = json.loads(json_bytes(build_snapshot(patient, molecular_only)))
    for version in ("1.0","1.1","1.2","1.3","1.4","1.5","1.6","1.7"):
        downgraded = {**data,"schema_version":version}
        with pytest.raises(ExportInputError, match="分子语义单元"):
            read_structured_data(json.dumps(downgraded))


@pytest.mark.parametrize("domain", ["molecular","cancer","lesion","cloud"])
def test_final_render_change_in_any_domain_scrubs_whole_four_domain_share(django_user_model, monkeypatch, domain):
    from apps.patients import share_views
    patient, _, lesion, cloud, scope, metric, _ = four_domains(django_user_model)
    made = create_share(patient, patient.account, scope)
    reader, _ = _patient(django_user_model,"four-domain-recipient")
    identity = exchange(reader,made.token)
    assert reader.get(f"/shared/{identity}/").status_code == 200
    render = share_views.render
    def changed(*args,**kwargs):
        response=render(*args,**kwargs)
        if args[1]=="patients/shared_detail.html": change_domain(patient,lesion,cloud,metric,domain)
        return response
    monkeypatch.setattr(share_views,"render",changed)
    response=reader.get(f"/shared/{identity}/")
    assert response.status_code == 410
    assert all(value not in response.content.decode() for value in ("NM_SYN.2","肺癌","=选定观察 <A>",FIRST_URL))
    made.share.refresh_from_db()
    assert made.share.snapshot == {} and made.share.invalidated_at is not None
    assert not made.share.cloud_sources.exists()
