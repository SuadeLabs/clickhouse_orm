from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import ModelWithConstraints2

operations = [migrations.AlterConstraints(ModelWithConstraints2)]
