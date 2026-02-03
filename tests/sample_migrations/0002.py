from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import Model1

operations = [migrations.DropTable(Model1)]
