from copy import deepcopy

import pytest

from tests.facts.test_molecular_schema import SAMPLES, negative


@pytest.mark.parametrize("key,value", [*SAMPLES, ("assay.negative_statement", negative())])
def test_original_value_controls_roundtrip_each_registered_molecular_kind(key, value):
    from apps.facts.molecular_forms import MolecularValueForm
    original = deepcopy(value)
    form = MolecularValueForm(key, value=value, initial={"raw_value": "SYN original transcription"})
    assert all(field.widget.__class__.__name__ != "HiddenInput" for field in form.fields.values())
    data = {k: v for k, v in form.initial.items() if k in form.fields and v is not None}
    bound = MolecularValueForm(key, data, value=value)
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data["value"] == original
    assert value == original


def test_optional_identity_component_unknown_is_not_treated_as_reviewed_absence():
    from apps.facts.molecular_forms import MolecularValueForm
    form = MolecularValueForm("variant.identity")
    data = {k: v for k, v in form.initial.items() if v is not None}
    data.update(raw_value="SYN1 c.1A>T", value_raw="SYN1 c.1A>T", kind="SMALL_VARIANT", status="COMPLETE", scope="SOMATIC",
                gene_state="PRINTED", gene_raw="SYN1", expression_state="PRINTED", expression_raw="c.1A>T")
    bound = MolecularValueForm("variant.identity", data)
    assert not bound.is_valid()
    data["status"] = "INCOMPLETE"
    bound = MolecularValueForm("variant.identity", data)
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data["value"]["transcripts"] == {"state": "UNKNOWN", "values": []}
