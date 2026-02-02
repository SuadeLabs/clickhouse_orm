from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import Model3

operations = [migrations.AlterTable(Model3)]
