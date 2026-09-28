from __future__ import annotations

import types
import unittest
from unittest import mock

from clickhouse_orm.compiler import ServerCapabilities, model_table_ref, qualified_name, substitute
from clickhouse_orm.database import DatabaseException
from clickhouse_orm.engines import Buffer, Distributed, Merge, MergeTree
from clickhouse_orm.fields import ArrayField, DateField, Int32Field, LowCardinalityField, StringField
from clickhouse_orm.models import BufferModel, DistributedModel, MergeModel, Model
from clickhouse_orm.system_models import SystemPart

from .base_test_with_data import TestCaseWithData

LEGACY = ServerCapabilities(has_custom_partitioning=False, has_codec_support=False, has_low_cardinality_support=False)


class ServerCapabilitiesTestCase(unittest.TestCase):
    def test_defaults_describe_modern_server(self):
        caps = ServerCapabilities()
        self.assertTrue(caps.has_custom_partitioning)
        self.assertTrue(caps.has_codec_support)
        self.assertTrue(caps.has_low_cardinality_support)

    def test_from_version(self):
        self.assertEqual(ServerCapabilities.from_version((25, 8, 33, 6)), ServerCapabilities())
        self.assertEqual(ServerCapabilities.from_version((1, 1, 54300)), LEGACY)
        self.assertEqual(
            ServerCapabilities.from_version((19, 0, 1)),
            ServerCapabilities(has_codec_support=False),
        )

    def test_immutable(self):
        with self.assertRaises(AttributeError):
            ServerCapabilities().has_codec_support = False


class SubstituteTestCase(unittest.TestCase):
    def test_no_placeholders(self):
        self.assertEqual(substitute("SELECT 1", "db"), "SELECT 1")

    def test_db_placeholder(self):
        self.assertEqual(substitute("SHOW TABLES FROM $db", "db"), "SHOW TABLES FROM `db`")

    def test_table_placeholder(self):
        self.assertEqual(substitute("SELECT * FROM $table", "db", SimpleModel), "SELECT * FROM `db`.`simplemodel`")
        self.assertEqual(substitute("SELECT * FROM $table", "db", SystemPart), "SELECT * FROM `system`.`parts`")

    def test_unknown_placeholders_are_kept(self):
        self.assertEqual(substitute("SELECT '$x' FROM $table", "db"), "SELECT '$x' FROM $table")

    def test_names(self):
        self.assertEqual(qualified_name("db", "t"), "`db`.`t`")
        self.assertEqual(model_table_ref("db", SimpleModel), "`db`.`simplemodel`")


class OfflineDDLTestCase(unittest.TestCase):
    """DDL generation must not require a connection to a ClickHouse server."""

    def setUp(self):
        patcher = mock.patch("requests.Session.post", side_effect=AssertionError("unexpected HTTP request"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_table_sql(self):
        self.assertEqual(
            SimpleModel.create_table_sql("db"),
            "CREATE TABLE IF NOT EXISTS `db`.`simplemodel` (\n"
            "    date Date DEFAULT '1970-01-01',\n"
            "    num Int32 DEFAULT 7 CODEC(ZSTD),\n"
            "    tags Array(LowCardinality(String)),\n"
            "    name LowCardinality(String)\n"
            ")\n"
            "ENGINE = MergeTree() PARTITION BY (toYYYYMM(`date`)) ORDER BY (date) SETTINGS index_granularity=8192",
        )

    def test_create_table_sql_legacy_server(self):
        with self.assertLogs("clickhouse_orm", "WARNING"):
            sql = SimpleModel.create_table_sql("db", LEGACY)
        self.assertEqual(
            sql,
            "CREATE TABLE IF NOT EXISTS `db`.`simplemodel` (\n"
            "    date Date DEFAULT '1970-01-01',\n"
            "    num Int32 DEFAULT 7,\n"
            "    tags Array(String),\n"
            "    name String\n"
            ")\n"
            "ENGINE = MergeTree(date, (date), 8192) ",
        )

    def test_drop_table_sql(self):
        self.assertEqual(SimpleModel.drop_table_sql("db"), "DROP TABLE IF EXISTS `db`.`simplemodel`")

    def test_buffer_model(self):
        self.assertEqual(
            SimpleBuffer.create_table_sql("db"),
            "CREATE TABLE IF NOT EXISTS `db`.`simplebuffer` AS `db`.`simplemodel` "
            "ENGINE = Buffer(`db`, `simplemodel`, 16, 10, 100, 10000, 1000000, 10000000, 100000000)",
        )

    def test_merge_model(self):
        self.assertEqual(
            SimpleMerge.create_table_sql("db"),
            "CREATE TABLE IF NOT EXISTS `db`.`simplemerge` (\n    date Date DEFAULT '1970-01-01'\n)\nENGINE = Merge(`db`, '^simple')",
        )

    def test_distributed_model(self):
        self.assertEqual(
            SimpleDistributed.create_table_sql("db"),
            "CREATE TABLE IF NOT EXISTS `db`.`simpledistributed` AS `db`.`simplemodel`\n"
            "ENGINE = Distributed(`cluster`, `db`, `simplemodel`)",
        )

    def test_custom_partitioning_unsupported(self):
        engine = MergeTree(partition_key=("date",), order_by=("date",))
        with self.assertRaises(DatabaseException):
            engine.create_table_sql("db", LEGACY)

    def test_field_get_sql(self):
        field = LowCardinalityField(StringField(), codec="ZSTD")
        self.assertEqual(field.get_sql(capabilities=ServerCapabilities()), "LowCardinality(String) CODEC(ZSTD)")
        with self.assertLogs("clickhouse_orm", "WARNING"):
            self.assertEqual(field.get_sql(), "String")


class DeprecatedDatabaseArgumentTestCase(unittest.TestCase):
    """Passing a `Database` to the DDL methods still works, but is deprecated."""

    def setUp(self):
        self.db = types.SimpleNamespace(db_name="db", capabilities=LEGACY)

    def test_model_create_table_sql(self):
        with self.assertWarns(DeprecationWarning), self.assertLogs("clickhouse_orm", "WARNING"):
            sql = SimpleModel.create_table_sql(self.db)
            self.assertEqual(sql, SimpleModel.create_table_sql("db", LEGACY))

    def test_model_drop_table_sql(self):
        with self.assertWarns(DeprecationWarning):
            sql = SimpleModel.drop_table_sql(self.db)
        self.assertEqual(sql, SimpleModel.drop_table_sql("db"))

    def test_engine_create_table_sql(self):
        with self.assertWarns(DeprecationWarning):
            sql = SimpleModel.engine.create_table_sql(self.db)
        self.assertEqual(sql, SimpleModel.engine.create_table_sql("db", LEGACY))

    def test_field_get_sql(self):
        field = Int32Field(codec="ZSTD")
        with self.assertWarns(DeprecationWarning):
            sql = field.get_sql(db=self.db)
        self.assertEqual(sql, "Int32")

    def test_warning_points_at_caller(self):
        with self.assertWarns(DeprecationWarning) as cm, self.assertLogs("clickhouse_orm", "WARNING"):
            SimpleModel.create_table_sql(self.db)
        self.assertEqual(cm.filename, __file__)


class DatabaseCapabilitiesTestCase(TestCaseWithData):
    def test_capabilities_from_server_version(self):
        self.assertEqual(self.database.capabilities, ServerCapabilities.from_version(self.database.server_version))
        self.assertEqual(self.database.has_codec_support, self.database.capabilities.has_codec_support)
        self.assertEqual(
            self.database.has_low_cardinality_support, self.database.capabilities.has_low_cardinality_support
        )


class SimpleModel(Model):
    date = DateField()
    num = Int32Field(default=7, codec="ZSTD")
    tags = ArrayField(LowCardinalityField(StringField()))
    name = LowCardinalityField(StringField())

    engine = MergeTree("date", ("date",))


class SimpleBuffer(BufferModel, SimpleModel):
    engine = Buffer(SimpleModel)


class SimpleMerge(MergeModel):
    date = DateField()

    engine = Merge("^simple")


class SimpleDistributed(DistributedModel, SimpleModel):
    engine = Distributed("cluster", SimpleModel)
