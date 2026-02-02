from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import AliasModel1, MaterializedModel1

operations = [migrations.AlterTable(MaterializedModel1), migrations.AlterTable(AliasModel1)]
