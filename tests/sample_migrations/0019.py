from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import ModelWithIndex2

operations = [migrations.AlterIndexes(ModelWithIndex2, reindex=True)]
