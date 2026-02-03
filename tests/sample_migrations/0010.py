from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import Model4Buffer

operations = [migrations.CreateTable(Model4Buffer)]
