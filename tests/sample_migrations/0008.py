from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import MaterializedModel

operations = [migrations.CreateTable(MaterializedModel)]
