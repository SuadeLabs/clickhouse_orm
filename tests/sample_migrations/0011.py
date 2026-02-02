from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import Model4BufferChanged

operations = [migrations.AlterTableWithBuffer(Model4BufferChanged)]
