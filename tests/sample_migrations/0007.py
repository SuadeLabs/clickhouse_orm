from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import EnumModel2

operations = [migrations.AlterTable(EnumModel2)]
