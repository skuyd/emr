"""Upgrade actual lesion/IHC main data through the joined molecular migration."""
import json

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder


@pytest.mark.django_db(transaction=True)
def test_actual_main_data_survives_joined_molecular_and_laterality_migrations(django_user_model):
    from tests.exports.test_lesion_selected_material import pair
    from tests.facts.molecular_factories import graph
    from tests.facts.pathology_factories import ihc_fixture, confirm_graph, review
    from apps.facts.clinical_readmodels import effective_field

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    MigrationExecutor(connection).migrate([("facts", "0003_pathology_context_anchors")])
    MigrationExecutor(connection).migrate([("facts", "0005_merge_pathology_laterality")])
    try:
        assert ("facts", "0004_molecular_context_anchors") not in MigrationRecorder(connection).applied_migrations()
        _, _, reports, lesion = pair(django_user_model,"main-migration-lesion")
        _, patient, _, _, ihc = ihc_fixture(django_user_model,"main-migration-ihc")
        confirm_graph(patient,ihc)
        watched=[*ihc.values(),*reports[0].fields.all()]
        current={str(f.pk):(effective_field(f)["usable"],effective_field(f)["current_source_token"]) for f in watched}
        assert effective_field(ihc["cps"])["usable"]
        tables=[*apps.get_app_config("facts").get_models(),*apps.get_app_config("lesions").get_models()]
        def rows():
            return json.dumps({m._meta.label:list(m.objects.order_by("pk").values()) for m in tables},sort_keys=True,default=str)
        before=rows()
        MigrationExecutor(connection).migrate(leaves)
        assert rows()==before
        assert ("facts","0006_merge_molecular_laterality") in MigrationRecorder(connection).applied_migrations()
        for field in watched:
            field.refresh_from_db()
            assert (effective_field(field)["usable"],effective_field(field)["current_source_token"])==current[str(field.pk)]
        _, molecular_patient, _, _, molecular = graph(django_user_model,"after-main-migration")
        for field in molecular.values():review(molecular_patient,field)
        assert effective_field(molecular["metric"])["usable"]
        lesion.refresh_from_db()
        assert lesion.original_name=="=选定观察 <A>"
    finally:
        MigrationExecutor(connection).migrate(leaves)
