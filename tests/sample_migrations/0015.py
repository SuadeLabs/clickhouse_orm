from __future__ import annotations

from clickhouse_orm import migrations

from ..test_migrations import Model2LowCardinality, Model4Compressed

operations = [migrations.AlterTable(Model4Compressed), migrations.AlterTable(Model2LowCardinality)]
