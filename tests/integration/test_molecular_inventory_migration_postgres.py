"""Real PostgreSQL storage width, preserved historical rows and new extraction."""
from copy import deepcopy

import pytest
from django.db import DataError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.services import collect_current
from apps.facts.clinical_readmodels import effective_field
from apps.facts.models import ClinicalExtraction
from tests.cancer_ordering.test_molecular_extraction_inventory import LEGACY, COMBINED
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def column_width():
    with connection.cursor() as cursor:
        cursor.execute("SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'facts_clinicalextraction' "
            "AND column_name = 'extractor_version'")
        return cursor.fetchone()[0]


def test_actual_legacy_identity_column_upgrade_preserves_records_and_allows_full_pipeline(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("Requires isolated PostgreSQL column constraints")
    _, patient, document, version, field, anchor = typed_fixture(django_user_model, name="legacy-identity-storage")
    extraction = ClinicalExtraction.objects.get(parsing_version=version)
    ClinicalExtraction.objects.filter(pk=extraction.pk).update(extractor_version=LEGACY)
    before = ClinicalExtraction.objects.values().get(pk=extraction.pk)
    fields = deepcopy(list(document.facts.order_by("pk").values()))
    anchor_state = effective_field(anchor)
    assert anchor_state["usable"]
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        MigrationExecutor(connection).migrate([("facts", "0006_merge_molecular_laterality")])
        assert column_width() == 40
        assert ClinicalExtraction.objects.values().get(pk=extraction.pk) == before
        MigrationExecutor(connection).migrate(leaves)
        assert column_width() == 128
        assert ClinicalExtraction.objects.values().get(pk=extraction.pk) == before
        assert list(document.facts.order_by("pk").values()) == fields
        assert effective_field(anchor)["usable"]
        assert effective_field(anchor)["current_source_token"] == anchor_state["current_source_token"]
        collect_current(patient, actor=patient.account)
        assert CancerCandidate.objects.get(source_fact=field).original_data["profile"] == "LUNG"

        _, new_patient, _, new_version, new_field, _ = typed_fixture(django_user_model, name="new-identity-storage")
        new_extraction = ClinicalExtraction.objects.get(parsing_version=new_version)
        assert new_extraction.extractor_version == COMBINED and len(COMBINED) > 40
        new_extraction.full_clean()
        assert all(run.status == "COMPLETE" for run in collect_current(new_patient, actor=new_patient.account))
        assert CancerCandidate.objects.get(source_fact=new_field).original_data["profile"] == "LUNG"
        # Once a real long identity exists, narrowing must fail atomically;
        # a schema rollback is never permission to truncate source identities.
        with pytest.raises(DataError):
            MigrationExecutor(connection).migrate([("facts", "0006_merge_molecular_laterality")])
        assert column_width() == 128
        new_extraction.refresh_from_db()
        assert new_extraction.extractor_version == COMBINED
        assert ("facts", "0007_expand_clinical_extractor_identity") in MigrationRecorder(connection).applied_migrations()
    finally:
        MigrationExecutor(connection).migrate(leaves)
