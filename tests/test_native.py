from __future__ import annotations

import datetime
import os
import unittest
from decimal import Decimal
from enum import Enum
from ipaddress import IPv4Address
from unittest import mock
from uuid import UUID

import pytest
import pytz

pytest.importorskip("clickhouse_driver")

from clickhouse_driver.defines import DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS
from clickhouse_driver.errors import ServerException

from clickhouse_orm.database import Database, DatabaseException, ServerError
from clickhouse_orm.driver import RequestsDriver
from clickhouse_orm.engines import Memory
from clickhouse_orm.fields import (
    ArrayField,
    DateField,
    DateTime64Field,
    DateTimeField,
    Decimal32Field,
    Enum8Field,
    FixedStringField,
    Float32Field,
    Float64Field,
    Int32Field,
    IPv4Field,
    IPv6Field,
    LowCardinalityField,
    NullableField,
    StringField,
    UInt64Field,
    UUIDField,
)
from clickhouse_orm.funcs import F
from clickhouse_orm.models import Model
from clickhouse_orm.native import NativeCodec, NativeDriver, NativeInsertData, NativeResponse, _Param, _shortest_float32

from .base_test_with_data import Person, TestCaseWithData
from .conftest import NATIVE_URL_ENV
from .fields.test_custom_fields import BooleanField


def _native_driver():
    # The URL is set by the conftest fixtures when they start the server, after this module is imported
    return NativeDriver.from_url(os.environ.get(NATIVE_URL_ENV, "clickhouse://localhost:9000"))


JERUSALEM = pytz.timezone("Asia/Jerusalem")


class Colour(Enum):
    red = 1
    green = 2


class Row(Model):
    name = StringField()
    num = Int32Field()

    engine = Memory()


class RowWithFuncDefault(Model):
    num = Int32Field()
    doubled = Int32Field(default=F.multiply(2, 3))

    engine = Memory()


class AllTypes(Model):
    id = Int32Field()
    string = StringField()
    fixed = FixedStringField(5)
    lc = LowCardinalityField(StringField())
    u64 = UInt64Field()
    f32 = Float32Field()
    f64 = Float64Field()
    dec = Decimal32Field(3)
    date = DateField()
    dt = DateTimeField()
    dt_tz = DateTimeField(timezone="Asia/Jerusalem")
    dt64 = DateTime64Field(precision=6)
    colour = Enum8Field(Colour)
    colours = ArrayField(Enum8Field(Colour))
    uuid = UUIDField()
    ip4 = IPv4Field()
    ip6 = IPv6Field()
    nullable = NullableField(Float32Field(), extra_null_values=[-1.0])
    nullable_dt = NullableField(DateTimeField())
    datetimes = ArrayField(DateTimeField())
    floats = ArrayField(Float32Field())
    strings = ArrayField(StringField())
    nullable_floats = ArrayField(NullableField(Float32Field()))
    flag = BooleanField()

    engine = Memory()


def _all_types_instances():
    moment = datetime.datetime(2020, 5, 17, 13, 45, 12)
    return [
        AllTypes(
            id=1,
            string="tab\there 'quoted' \\ back",
            fixed="abc",
            lc="low",
            u64=2**64 - 1,
            f32=1.7,
            f64=2.5,
            dec=Decimal("3.14159"),
            date=datetime.date(2021, 3, 4),
            dt=moment,
            dt_tz=JERUSALEM.localize(moment),
            dt64=moment.replace(microsecond=123456),
            colour=Colour.green,
            colours=[Colour.red, Colour.green],
            uuid=UUID("12345678-1234-5678-1234-567812345678"),
            ip4="1.2.3.4",
            ip6="::1",
            nullable=-1.0,
            nullable_dt=moment,
            datetimes=[moment, moment + datetime.timedelta(days=1)],
            floats=[0.1, 2.5],
            strings=["a'b", "back\\slash\\", "\\'", "tab\t", "NULL", "", "[', ']"],
            nullable_floats=[0.1, None],
            flag=True,
        ),
        AllTypes(id=2, nullable=0.3, flag=False),
    ]


def _client_mock():
    client = mock.Mock()
    client.execute.return_value = ([(1,)], [("x", "UInt8")])
    return client


class NativeDriverTestCase(unittest.TestCase):
    def setUp(self):
        self.driver = NativeDriver("example.com", port=9001, user="me", settings={"max_threads": 2})
        self.client = _client_mock()
        patcher = mock.patch("clickhouse_orm.native.Client", return_value=self.client)
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)

    def test_client_configuration(self):
        self.driver.client()
        self.client_class.assert_called_once_with(
            "example.com", port=9001, user="me", settings={"max_threads": 2, "server_side_params": True}
        )

    def test_client_per_database(self):
        self.assertIs(self.driver.client("db"), self.driver.client("db"))
        self.driver.client(None)
        self.assertEqual(self.client_class.call_count, 2)
        self.assertEqual(self.client_class.call_args_list[0].kwargs["database"], "db")
        self.assertNotIn("database", self.client_class.call_args_list[1].kwargs)
        self.driver.disconnect()
        self.assertEqual(self.client.disconnect.call_count, 2)

    def test_database_cannot_be_configured(self):
        with self.assertRaises(ValueError):
            NativeDriver(database="db")

    def test_from_url(self):
        driver = NativeDriver.from_url("clickhouse://me:secret@example.com:9001/ignored?secure=True")
        self.assertEqual(driver.host, "example.com")
        self.assertEqual(
            driver.client_kwargs,
            {
                "port": 9001,
                "user": "me",
                "password": "secret",
                "secure": True,
                "settings": {"server_side_params": True},
            },
        )

    def test_send(self):
        response = self.driver.send("SELECT 1", settings={"database": "db", "readonly": "1"}, params={"p": "a'b"})
        self.assertEqual(self.client_class.call_args.kwargs["database"], "db")
        ((query, params), kwargs) = self.client.execute.call_args
        self.assertEqual(query, "SELECT 1")
        self.assertEqual({name: str(value) for name, value in params.items()}, {"p": "a\\'b"})
        self.assertEqual(kwargs, {"with_column_types": True, "settings": {"readonly": "1"}})
        self.assertEqual((response.columns, response.rows), ([("x", "UInt8")], [(1,)]))
        self.assertEqual(self.driver.scalar("SELECT 1"), "1")

    def test_params_unsupported_by_server(self):
        unknown_parameter = ServerException("DB::Exception: Query parameter `p` was not set", code=456)
        self.client.execute.side_effect = [unknown_parameter, ([(1,)], [("1", "UInt8")])]
        self.client.connection.server_info.used_revision = DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS - 1
        self.client.connection.server_info.version_tuple.return_value = (21, 3, 20)
        with self.assertRaisesRegex(DatabaseException, "ClickHouse 21.3.20 does not support query parameters"):
            self.driver.send("SELECT {p:String}", params={"p": "a"})

    def test_unknown_param(self):
        unknown_parameter = ServerException("DB::Exception: Query parameter `q` was not set", code=456)
        self.client.execute.side_effect = [unknown_parameter, ([(1,)], [("1", "UInt8")])]
        self.client.connection.server_info.used_revision = DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS
        with self.assertRaises(ServerError) as cm:
            self.driver.send("SELECT {q:String}", params={"p": "a"})
        self.assertEqual(cm.exception.code, 456)

    def test_send_without_params(self):
        self.driver.send("SELECT 1")
        self.assertIsNone(self.client.execute.call_args.args[1])

    def test_send_insert_data(self):
        rows = []
        self.client.execute.side_effect = lambda query, data, settings: rows.extend(data)
        response = self.driver.send("INSERT INTO t (x) VALUES", data=iter([(1,), (2,)]))
        self.assertEqual(rows, [(1,), (2,)])
        self.assertEqual(response.text, "")

    def test_send_insert_block_size(self):
        self.driver.send("INSERT INTO t (x) VALUES", data=NativeInsertData(iter([(1,)]), 10))
        self.assertEqual(self.client.execute.call_args.kwargs["settings"], {"insert_block_size": 10})

    def test_server_error(self):
        self.client.execute.side_effect = ServerException("DB::Exception: Syntax error", code=62)
        with self.assertRaises(ServerError) as cm:
            self.driver.send("SELEC 1")
        self.assertEqual((cm.exception.code, cm.exception.message), (62, "Syntax error"))

    def test_float32_normalized(self):
        self.client.execute.return_value = (
            [(1.7000000476837158, [0.10000000149011612, None], None, 1.7000000476837158)],
            [("a", "Float32"), ("b", "Array(Nullable(Float32))"), ("c", "Nullable(Float32)"), ("d", "Float64")],
        )
        self.assertEqual(self.driver.send("SELECT").rows, [(1.7, [0.1, None], None, 1.7000000476837158)])

    def test_param_quoting(self):
        self.assertEqual(str(_Param("a\\tb'c")), "a\\\\tb\\'c")


class NativeHelpersTestCase(unittest.TestCase):
    def test_shortest_float32(self):
        for value in (1.7, 0.1, 1e-7, 3.4e38, 123456.79, -2.5, 0.0):
            self.assertEqual(_shortest_float32(Float32Field().to_python(_widen(value), None)), value)
        self.assertEqual(_shortest_float32(float("inf")), float("inf"))

    def test_response_text(self):
        response = NativeResponse(
            [],
            [
                ("a\tb", None, True, 1.5, Decimal("2.50"), datetime.datetime(2020, 1, 2, 3, 4, 5)),
                ([1, None], ["x'y"], (1, "z"), {"k": 1}, datetime.date(2020, 1, 2), IPv4Address("1.2.3.4")),
            ],
        )
        self.assertEqual(
            response.text,
            "a\\tb\t\\N\ttrue\t1.5\t2.50\t2020-01-02 03:04:05\n[1,NULL]\t['x\\'y']\t(1,'z')\t{'k':1}\t2020-01-02\t1.2.3.4\n",
        )
        self.assertEqual(NativeResponse([], []).text, "")


def _widen(value):
    import struct

    return struct.unpack("f", struct.pack("f", value))[0]


class NativeCodecTestCase(unittest.TestCase):
    def setUp(self):
        self.codec = NativeCodec()

    def test_no_select_format(self):
        self.assertIsNone(self.codec.select_format)

    def test_encode_inserts(self):
        ((statement, rows),) = self.codec.encode_inserts(Row, [Row(name="a", num=1), Row(name="b", num=2)])
        self.assertEqual(statement, "INSERT INTO $table (`name`,`num`) VALUES")
        self.assertEqual(list(rows), [("a", 1), ("b", 2)])

    def test_encode_inserts_block_size(self):
        ((_, data),) = self.codec.encode_inserts(Row, [Row(num=1)], batch_size=10)
        self.assertEqual(data.block_size, 10)

    def test_encode_is_lazy(self):
        def instances():
            yield Row(num=1)
            raise AssertionError("instances should be read with the rows")

        ((_, rows),) = self.codec.encode_inserts(Row, instances())
        self.assertEqual(next(rows), ("", 1))

    def test_encode_values(self):
        (instance, _) = _all_types_instances()
        ((_, rows),) = self.codec.encode_inserts(AllTypes, [instance])
        values = dict(zip(AllTypes.fields(writable=True), next(rows)))
        self.assertEqual(values["colour"], "green")
        self.assertEqual(values["colours"], ["red", "green"])
        self.assertIsNone(values["nullable"])  # extra NULL value
        self.assertEqual(values["floats"], [0.1, 2.5])
        self.assertEqual(values["flag"], 1)  # custom field, parsed from its text as a UInt8
        self.assertIs(values["dt"], instance.dt)

    def test_encode_func_defaults(self):
        instances = [RowWithFuncDefault(num=1), RowWithFuncDefault(num=2), RowWithFuncDefault(num=3, doubled=0)]
        inserts = [
            (statement, list(rows)) for statement, rows in self.codec.encode_inserts(RowWithFuncDefault, instances)
        ]
        self.assertEqual(
            inserts,
            [
                ("INSERT INTO $table (`num`) VALUES", [(1,), (2,)]),
                ("INSERT INTO $table (`num`,`doubled`) VALUES", [(3, 0)]),
            ],
        )

    def test_decode(self):
        response = NativeResponse([("num", "Int32"), ("name", "String")], [(1, "a"), (2, "b")])
        rows = list(self.codec.decode(response, Row))
        self.assertEqual([(r.num, r.name) for r in rows], [(1, "a"), (2, "b")])

    def test_decode_ad_hoc_model(self):
        (row,) = self.codec.decode(NativeResponse([("x", "UInt8"), ("e", "Enum8('a' = 1)")], [(5, "a")]))
        self.assertEqual((row.x, row.e.name), (5, "a"))

    def test_decode_datetimes(self):
        naive = datetime.datetime(2020, 1, 1, 12, 0)
        response = NativeResponse(
            [("dt", "DateTime"), ("dt_tz", "DateTime('Asia/Jerusalem')"), ("datetimes", "Array(DateTime)")],
            [(naive, JERUSALEM.localize(naive), [naive])],
        )
        (row,) = self.codec.decode(response, AllTypes, pytz.timezone("Europe/Madrid"))
        # Values are used as returned by clickhouse-driver: naive unless the column has a timezone
        self.assertEqual(row.dt, naive)
        self.assertIsNone(row.dt.tzinfo)
        self.assertEqual(row.dt_tz, JERUSALEM.localize(naive))
        self.assertEqual(row.dt_tz.tzinfo.zone, "Asia/Jerusalem")
        self.assertEqual(row.datetimes, [naive])

    def test_decode_custom_field(self):
        timezones = []

        class Recording(BooleanField):
            def to_python(self, value, timezone_in_use):
                timezones.append(timezone_in_use)
                return super().to_python(value, timezone_in_use)

        class WithCustom(Model):
            flag = Recording()

        timezones.clear()  # the default value was converted when creating the class
        (row,) = self.codec.decode(NativeResponse([("flag", "UInt8")], [(1,)]), WithCustom, JERUSALEM)
        self.assertIs(row.flag, True)
        self.assertEqual(timezones[0], JERUSALEM)

    def test_decode_rows(self):
        response = NativeResponse([("x", "UInt8")], [(1,), (2,)])
        result = self.codec.decode_rows(response)
        self.assertEqual((result.columns, list(result)), ([("x", "UInt8")], [(1,), (2,)]))


class DatabaseDriverOptionTestCase(unittest.TestCase):
    def test_http_options_conflict_with_driver(self):
        for kwargs in ({"db_url": "http://localhost:8123/"}, {"username": "me"}, {"password": "secret"}):
            with self.assertRaises(ValueError):
                Database("db", driver=NativeDriver(), **kwargs)


class _NativeTestCase(TestCaseWithData):
    """Runs against the server's native protocol, whichever driver `--driver` selects for the other databases."""

    def setUp(self):
        super().setUp()
        self.native = Database(self.database.db_name, driver=_native_driver(), log_statements=True)
        self.http = Database(self.database.db_name, driver=RequestsDriver(Database._default_url))

    def tearDown(self):
        self.native.driver.disconnect()
        super().tearDown()


class NativeDatabaseTestCase(_NativeTestCase):
    def test_driver_and_codec(self):
        self.assertIsInstance(self.native.codec, NativeCodec)
        self.assertIsNone(self.native.db_url)
        self.assertEqual(self.native.server_version, self.http.server_version)
        self.assertEqual(self.native.server_timezone, self.http.server_timezone)

    def test_insert_and_select(self):
        self.native.create_table(Person)
        self.native.insert(self._sample_data())
        self.assertEqual(self.native.count(Person), 100)
        people = list(Person.objects_in(self.native).filter(first_name="Whitney").order_by("last_name"))
        self.assertEqual([p.last_name for p in people], ["Durham", "Scott"])
        self.assertTrue(all(p.get_database() is self.native for p in people))

    def test_raw(self):
        self._insert_all()
        self.assertEqual(
            self.native.raw("SELECT * FROM `test-db`.person WHERE first_name = 'Whitney' ORDER BY last_name"),
            "Whitney\tDurham\t1977-09-15\t1.72\t\\N\nWhitney\tScott\t1971-07-04\t1.7\t\\N\n",
        )
        self.assertEqual(self.native.raw("SELECT 1 WHERE 0"), "")

    def test_params(self):
        self._insert_all()
        value = "O'Brien\t\\ \n%s %(x)s"
        try:
            rows = self.native.select_rows("SELECT {v:String}, {n:Nullable(Int8)}", params={"v": value, "n": None})
        except DatabaseException as e:
            self.skipTest(str(e))
        self.assertEqual(list(rows), [(value, None)])
        qs = Person.objects_in(self.native).filter(first_name="Whitney", height__gt=1.71).parameterized()
        self.assertEqual([p.last_name for p in qs], ["Durham"])

    def test_subquery(self):
        self._insert_all()
        results = []
        for database in (self.native, self.http):
            subquery = Person.objects_in(database).filter(first_name="Whitney").only("last_name")
            qs = Person.objects_in(database).filter(last_name__in=subquery).order_by("first_name")
            results.append([p.first_name for p in qs])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].count("Whitney"), 2)

    def test_queries_while_iterating(self):
        self._insert_all()
        # Results are read in full, so the connection is free for other queries
        for person in Person.objects_in(self.native).filter(first_name="Whitney"):
            self.assertEqual(Person.objects_in(self.native).filter(last_name=person.last_name).count() > 0, True)

    def test_server_error(self):
        with self.assertRaises(ServerError) as cm:
            self.native.raw("SELECT * FROM no_such_table")
        self.assertEqual(cm.exception.code, 60)

    def test_readonly(self):
        self._insert_all()
        readonly = Database(self.database.db_name, driver=_native_driver(), readonly=True)
        self.assertEqual(readonly.count(Person), 100)
        with self.assertRaises(ServerError) as cm:
            readonly.drop_table(Person)
        self.assertEqual(cm.exception.code, 164)
        readonly.driver.disconnect()


class CrossDriverTestCase(_NativeTestCase):
    """Model instances and rows must not depend on the driver that reads or writes them."""

    def setUp(self):
        super().setUp()
        self.http.create_table(AllTypes)

    def _read(self, database):
        return [instance.to_dict() for instance in AllTypes.objects_in(database).order_by("id")]

    def _check_models(self, writer):
        instances = _all_types_instances()
        writer.insert(instances)
        self.assertEqual(self._read(self.native), self._read(self.http))
        # Default datetimes are absolute (the epoch), so columns without a timezone read them back as naive
        epoch = DateTimeField.class_default.astimezone(self.http.server_timezone).replace(tzinfo=None)
        instances[1].dt = instances[1].dt64 = epoch
        self.assertEqual(self._read(self.http), [instance.to_dict() for instance in instances])

    def test_insert_http(self):
        self._check_models(self.http)

    def test_insert_native(self):
        self._check_models(self.native)

    def test_aware_values_in_naive_columns(self):
        moment = pytz.timezone("Asia/Tokyo").localize(datetime.datetime(2020, 5, 17, 13, 45, 12))
        expected = moment.astimezone(self.http.server_timezone).replace(tzinfo=None)
        for writer in (self.http, self.native):
            writer.insert([AllTypes(id=1, dt=moment, dt64=moment, datetimes=[moment])])
            for reader in (self.http, self.native):
                (instance,) = AllTypes.objects_in(reader)
                self.assertEqual((instance.dt, instance.dt64, instance.datetimes), (expected, expected, [expected]))
            self.http.raw("TRUNCATE TABLE $db.alltypes")

    def test_insert_func_defaults(self):
        self.native.create_table(RowWithFuncDefault)
        self.native.insert([RowWithFuncDefault(num=1), RowWithFuncDefault(num=2, doubled=0)])
        rows = RowWithFuncDefault.objects_in(self.http).order_by("num")
        self.assertEqual([(r.num, r.doubled) for r in rows], [(1, 6), (2, 0)])

    def test_select_rows(self):
        self.http.insert(_all_types_instances())
        flag = "toBool(flag)" if self.http.server_version >= (21, 12) else "flag"
        query = f"SELECT * EXCEPT (flag), {flag}, CAST(1 AS Int128), [[1]], NULL FROM $db.alltypes ORDER BY id"
        http_result = self.http.select_rows(query)
        native_result = self.native.select_rows(query)
        self.assertEqual(native_result.columns, http_result.columns)
        http_rows, native_rows = list(http_result), list(native_result)
        # Types which the TSV codec cannot parse (such as nested arrays) are returned as text
        self.assertEqual([row[-2] for row in http_rows], ["[[1]]"] * 2)
        self.assertEqual([row[-2] for row in native_rows], [[[1]]] * 2)
        self.assertEqual([row[:-2] + row[-1:] for row in native_rows], [row[:-2] + row[-1:] for row in http_rows])
