from datetime import datetime, timezone as datetime_timezone
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_legacy_effective_local_time_is_backfilled_without_utc_conversion():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [(app, '0002_dailyrecordexportsource_dailyrecordsharesource' if app == 'self_records' else name)
                for app, name in leaves]
    try:
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        account = old.get_model('accounts', 'Account').objects.create(
            phone_hash='a' * 64, phone_encrypted='synthetic')
        patient = old.get_model('patients', 'Patient').objects.create(
            account_id=account.pk, display_name='迁移测试患者')
        content = {'kind': 'WEIGHT', 'local_time': '2026-10-25T02:30', 'raw_value': '60',
                   'raw_unit': 'kg', 'time_precision': 'MINUTE', 'timezone': 'Europe/Berlin',
                   'utc_offset': '+01:00'}
        instant = datetime(2026, 10, 25, 1, 30, tzinfo=datetime_timezone.utc)
        record = old.get_model('self_records', 'DailyRecord').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, updated_by_id=account.pk,
            creation_key=uuid4(), creation_fingerprint='b' * 64, original_data=content,
            current_data=content, kind='WEIGHT', measured_at=instant)
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        migrated = current.get_model('self_records', 'DailyRecord').objects.get(pk=record.pk)
        assert migrated.record_date.isoformat() == '2026-10-25'
        assert migrated.record_time.strftime('%H:%M') == '02:30'
        assert migrated.measured_at == instant
        assert migrated.current_data == content
    finally:
        MigrationExecutor(connection).migrate(leaves)
