from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("clickhouse_connect")

from clickhouse_orm.alchemy import ModelSelect, build_table, field_to_sqla_type  # noqa: E402
from clickhouse_orm.compiler import ServerCapabilities  # noqa: E402
from clickhouse_orm.fields import (  # noqa: E402
    ArrayField,
    DateField,
    Enum8Field,
    LowCardinalityField,
    NullableField,
    StringField,
    UInt32Field,
)

from .base_test_with_data import Person, TestCaseWithData, data  # noqa: E402


class FieldToSqlaTypeTestCase(TestCaseWithData):
    """Checks that field types are mapped to matching SQLAlchemy/ClickHouse type strings."""

    def _assert_type_str(self, field, expected):
        self.assertEqual(str(field_to_sqla_type(field, capabilities=ServerCapabilities())), expected)

    def test_simple_field(self):
        self._assert_type_str(UInt32Field(), "UInt32")
        self._assert_type_str(StringField(), "String")
        self._assert_type_str(DateField(), "Date")

    def test_nullable_field(self):
        self._assert_type_str(NullableField(UInt32Field()), "Nullable(UInt32)")

    def test_low_cardinality_field(self):
        self._assert_type_str(LowCardinalityField(StringField()), "LowCardinality(String)")

    def test_array_field(self):
        self._assert_type_str(ArrayField(UInt32Field()), "Array(UInt32)")

    def test_enum_field(self):
        from enum import Enum

        class Color(Enum):
            red = 1
            blue = 2

        field_type = str(field_to_sqla_type(Enum8Field(Color)))
        self.assertTrue(field_type.startswith("Enum8("))
        self.assertIn("'red' = 1", field_type)
        self.assertIn("'blue' = 2", field_type)


class GetTableTestCase(TestCaseWithData):
    def test_get_table_columns(self):
        table = self.database.get_table(Person)
        self.assertEqual(table.name, "person")
        self.assertEqual(table.schema, self.database.db_name)
        self.assertEqual(set(c.name for c in table.columns), set(Person.fields().keys()))

    def test_get_table_is_cached(self):
        table1 = self.database.get_table(Person)
        table2 = self.database.get_table(Person)
        self.assertIs(table1, table2)

    def test_build_table_is_not_cached(self):
        # build_table() always creates a fresh Table; Database.get_table() is the caching entry point.
        table = build_table(Person, self.database.metadata.__class__(), schema="other")
        self.assertEqual(table.schema, "other")
        self.assertIsNot(table, self.database.get_table(Person))


class DatabaseQueryTestCase(TestCaseWithData):
    """Exercises `Database.query()` against a real ClickHouse server."""

    def setUp(self):
        super().setUp()
        self.database.insert(self._sample_data())

    def test_query_returns_model_instances(self):
        qs = self.database.query(Person)
        results = list(qs)
        self.assertEqual(len(results), len(data))
        for instance in results:
            self.assertIsInstance(instance, Person)
            self.assertIs(instance.get_database(), self.database)

    def test_query_returns_model_select(self):
        qs = self.database.query(Person)
        self.assertIsInstance(qs, ModelSelect)

    def test_query_filtering_with_sqlalchemy_expressions(self):
        qs = self.database.query(Person)
        qs = qs.where(qs.table.c.first_name == "Connor")
        results = list(qs)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].first_name, "Connor")
        self.assertEqual(results[0].last_name, "Jenkins")

    def test_query_order_by_and_limit(self):
        qs = self.database.query(Person)
        qs = qs.order_by(qs.table.c.height).limit(3)
        results = list(qs)
        self.assertEqual(len(results), 3)
        heights = [r.height for r in results]
        self.assertEqual(heights, sorted(heights))

    def test_query_nullable_and_low_cardinality_field_values(self):
        qs = self.database.query(Person)
        qs = qs.where(qs.table.c.first_name == "Abdul")
        results = list(qs)
        self.assertEqual(len(results), 1)
        person = results[0]
        self.assertEqual(person.passport, 35052255)
        self.assertEqual(person.last_name, "Hester")

        qs = self.database.query(Person)
        qs = qs.where(qs.table.c.first_name == "Adena")
        person = next(iter(qs))
        self.assertIsNone(person.passport)  # not set in sample data -> NULL

    def test_query_selected_columns(self):
        from sqlalchemy import select

        table = self.database.get_table(Person)
        stmt = select(table.c.first_name, table.c.last_name).where(table.c.first_name == "Connor")
        with self.database.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(stmt).mappings()]
        self.assertEqual(rows, [{"first_name": "Connor", "last_name": "Jenkins"}])

    def test_insert_still_uses_tsv_path(self):
        # Database.insert() (TSV over HTTP) remains the recommended fast bulk-insert path, unaffected by the
        # SQLAlchemy backend.
        before = self.database.count(Person)
        self.database.insert([Person(**data[0])])
        after = self.database.count(Person)
        self.assertEqual(after, before + 1)
