from __future__ import annotations

import datetime
import unittest
from unittest import mock

import pytest
import pytz

from clickhouse_orm.codec import Codec, RowResult, TSVCodec
from clickhouse_orm.database import ServerError
from clickhouse_orm.engines import Memory
from clickhouse_orm.fields import DateTimeField, Int32Field, StringField
from clickhouse_orm.funcs import F
from clickhouse_orm.models import Model

from .base_test_with_data import Person, TestCaseWithData


def _response(lines):
    """A response with the given body lines, as returned by `RequestsDriver`."""
    return mock.Mock(iter_lines=mock.Mock(return_value=iter(lines)))


class TSVCodecTestCase(unittest.TestCase):
    def setUp(self):
        self.codec = TSVCodec()

    def test_codec_is_abstract(self):
        with self.assertRaises(TypeError):
            Codec()

    def test_formats(self):
        self.assertEqual(self.codec.select_format, "TabSeparatedWithNamesAndTypes")
        self.assertEqual(self.codec.insert_format(Row), "TabSeparated")
        self.assertEqual(self.codec.insert_format(RowWithFuncDefault), "TSKV")

    def test_encode_inserts(self):
        rows = [Row(name="a", num=1)]
        ((statement, data),) = self.codec.encode_inserts(Row, rows)
        self.assertEqual(statement, "INSERT INTO $table (`name`,`num`) FORMAT TabSeparated")
        self.assertEqual(list(data), [b"a\t1\n"])

    def test_encode(self):
        rows = [Row(name="a\tb", num=1), Row(name="c", num=2)]
        self.assertEqual(list(self.codec.encode(Row, rows)), [b"a\\tb\t1\nc\t2\n"])

    def test_encode_tskv(self):
        rows = [RowWithFuncDefault(num=3)]
        self.assertEqual(list(self.codec.encode(RowWithFuncDefault, rows)), [b"num=3\n"])

    def test_encode_batches(self):
        rows = [Row(name=str(n), num=n) for n in range(5)]
        chunks = list(self.codec.encode(Row, rows, batch_size=2))
        self.assertEqual(chunks, [b"0\t0\n1\t1\n", b"2\t2\n3\t3\n", b"4\t4\n"])

    def test_encode_is_lazy(self):
        consumed = []

        def gen():
            for n in range(3):
                consumed.append(n)
                yield Row(num=n)

        chunks = self.codec.encode(Row, gen(), batch_size=1)
        self.assertEqual(consumed, [])
        next(chunks)
        self.assertEqual(consumed, [0])

    def test_encode_empty(self):
        self.assertEqual(list(self.codec.encode(Row, [])), [])

    def test_decode(self):
        lines = [b"num\tname", b"Int32\tString", b"1\ta\\tb", b"", b"2\tc"]
        rows = list(self.codec.decode(_response(lines), Row))
        self.assertEqual([(r.name, r.num) for r in rows], [("a\tb", 1), ("c", 2)])
        self.assertTrue(all(isinstance(r, Row) for r in rows))

    def test_decode_ad_hoc_model(self):
        lines = [b"x\ty", b"UInt8\tString", b"5\thello"]
        (row,) = self.codec.decode(_response(lines))
        self.assertEqual((row.x, row.y), (5, "hello"))
        self.assertNotIsInstance(row, Row)

    def test_decode_timezone(self):
        lines = [b"ts", b"DateTime", b"2020-01-01 12:00:00"]
        # Columns without a timezone are naive, whatever the server's timezone
        (row,) = self.codec.decode(_response(lines), WithDateTime, pytz.timezone("Asia/Jerusalem"))
        self.assertEqual(row.ts, datetime.datetime(2020, 1, 1, 12, 0))
        lines = [b"ts", b"DateTime(\\'Asia/Tokyo\\')", b"2020-01-01 12:00:00"]
        (row,) = self.codec.decode(_response(lines), WithDateTime, pytz.timezone("Asia/Jerusalem"))
        self.assertEqual(row.ts, pytz.timezone("Asia/Tokyo").localize(datetime.datetime(2020, 1, 1, 12, 0)))
        self.assertEqual(row.ts.tzinfo.zone, "Asia/Tokyo")


# Captured from a ClickHouse 25.8 server
ROWS_RESPONSE = [
    b"dt\tdt_utc\te\tfs\traw\ts\tn\tb\tbig\tt\tsa\tarr",
    b"DateTime\tDateTime(\\'UTC\\')\tEnum8(\\'a\\' = 1, \\'b\\' = 2)\tFixedString(4)\tString\tString"
    b"\tNullable(Int32)\tBool\tUInt256\tTuple(UInt8, String)\tSimpleAggregateFunction(sum, UInt64)\tArray(UInt8)",
    b"2020-01-01 12:00:00\t2020-01-01 12:00:00\tb\tab\\0\\0\t\xff\ta\\tb\t\\N\ttrue\t5\t(1,'a')\t3\t[1,2]",
]


class TSVCodecRowsTestCase(unittest.TestCase):
    """Row values follow the Python types returned by clickhouse_driver."""

    def setUp(self):
        self.tz = pytz.timezone("Asia/Jerusalem")
        self.result = TSVCodec().decode_rows(_response(ROWS_RESPONSE), self.tz)

    def test_columns(self):
        self.assertIsInstance(self.result, RowResult)
        self.assertEqual(self.result.columns[:2], [("dt", "DateTime"), ("dt_utc", "DateTime('UTC')")])
        self.assertEqual(
            self.result.column_names, ["dt", "dt_utc", "e", "fs", "raw", "s", "n", "b", "big", "t", "sa", "arr"]
        )

    def test_values(self):
        (row,) = self.result
        self.assertEqual(
            row,
            (
                datetime.datetime(2020, 1, 1, 12, 0),
                pytz.utc.localize(datetime.datetime(2020, 1, 1, 12, 0)),
                "b",
                "ab",
                b"\xff",
                "a\tb",
                None,
                True,
                5,
                (1, "a"),
                3,
                [1, 2],
            ),
        )
        self.assertIsNone(row[0].tzinfo)
        self.assertIs(type(row), tuple)

    def test_array_elements(self):
        lines = [
            b"e\td\tn\ts",
            b"Array(Enum8(\\'a\\' = 1, \\'b\\' = 2))\tArray(DateTime)\tArray(Nullable(UInt8))\tArray(String)",
            b"['a','b']\t['2020-01-01 12:00:00']\t[1,NULL]\t['x','y z']",
        ]
        ((enums, datetimes, nullables, strings),) = TSVCodec().decode_rows(_response(lines), self.tz)
        self.assertEqual(enums, ["a", "b"])
        self.assertEqual(datetimes, [datetime.datetime(2020, 1, 1, 12, 0)])
        self.assertEqual(nullables, [1, None])
        self.assertEqual(strings, ["x", "y z"])

    def test_quoted_array_elements(self):
        # Arrays, tuples and maps are written in the quoted format, and are not escaped for TSV
        lines = [
            b"s\tn\tt\tagg",
            b"Array(Nullable(String))\tArray(Nullable(UInt8))\tTuple(String, String)\t"
            b"SimpleAggregateFunction(groupArrayArray, Array(String))",
            b"['a\\'b','\\\\','NULL',NULL,'\\t']\t[NULL]\t('a\\'b','c\\\\d')\t['\\\\']",
        ]
        ((strings, nulls, pair, agg),) = TSVCodec().decode_rows(_response(lines), self.tz)
        self.assertEqual(strings, ["a'b", "\\", "NULL", None, "\t"])
        self.assertEqual(nulls, [None])
        self.assertEqual(pair, ("a'b", "c\\d"))
        self.assertEqual(agg, ["\\"])

    def test_tuple_elements(self):
        # Elements are typed like values of their types, including in arrays of (named) tuples
        lines = [
            b"t	at	one	unsupported",
            b"Tuple(a Enum8(\\'x\\' = 1), b Nullable(DateTime), c Array(UInt8), d Tuple(String, Bool))"
            b"	Array(Tuple(UInt8, String))	Tuple(UInt8)	Tuple(Map(String, UInt8))",
            b"('x',NULL,[1,2],('(,)',true))	[(1,'a'),(2,'b')]	(1)	({'k':1})",
        ]
        ((value, array, single, unsupported),) = TSVCodec().decode_rows(_response(lines), self.tz)
        self.assertEqual(value, ("x", None, [1, 2], ("(,)", True)))
        self.assertEqual(array, [(1, "a"), (2, "b")])
        self.assertEqual(single, (1,))
        # Elements of types which the TSV codec cannot parse are returned as text
        self.assertEqual(unsupported, ("{'k':1}",))

    def test_explicit_timezone_column(self):
        lines = [b"dt", b"DateTime(\\'Asia/Tokyo\\')", b"2020-01-01 12:00:00"]
        ((value,),) = TSVCodec().decode_rows(_response(lines))
        self.assertEqual(value, pytz.timezone("Asia/Tokyo").localize(datetime.datetime(2020, 1, 1, 12, 0)))
        self.assertEqual(value.tzinfo.zone, "Asia/Tokyo")

    def test_duplicate_column_names(self):
        result = TSVCodec().decode_rows(_response([b"1\t1", b"UInt8\tUInt8", b"1\t1"]))
        self.assertEqual(result.columns, [("1", "UInt8"), ("1", "UInt8")])
        self.assertEqual(list(result), [(1, 1)])

    def test_empty_result_and_totals(self):
        result = TSVCodec().decode_rows(_response([b"k\tc", b"UInt8\tUInt64", b"0\t2", b"1\t2", b"", b"0\t4"]))
        self.assertEqual(list(result), [(0, 2), (1, 2), (0, 4)])
        result = TSVCodec().decode_rows(_response([b"x", b"UInt8"]))
        self.assertEqual((result.columns, list(result)), ([("x", "UInt8")], []))

    def test_unsupported_types_as_text(self):
        lines = [b"m\tarray", b"Map(String, UInt8)\tArray(Map(String, UInt8))", b"{'a':1}\t[{'b':2}]"]
        self.assertEqual(list(TSVCodec().decode_rows(_response(lines))), [("{'a':1}", ["{'b':2}"])])

    def test_nested_arrays(self):
        lines = [b"nested", b"Array(Array(Nullable(String)))", b"[['a',NULL],[],['[\\'b\\']']]"]
        self.assertEqual(list(TSVCodec().decode_rows(_response(lines))), [([["a", None], [], ["['b']"]],)])

    def test_is_lazy(self):
        def lines():
            yield from ROWS_RESPONSE[:2]
            raise AssertionError("rows should not be read before iteration")

        result = TSVCodec().decode_rows(_response(lines()))
        self.assertEqual(len(result.columns), 12)


class DatabaseCodecTestCase(TestCaseWithData):
    @pytest.mark.http_only
    def test_default_codec(self):
        self.assertIsInstance(self.database.codec, TSVCodec)

    @pytest.mark.http_only
    def test_insert_and_select_use_codec(self):
        codec = self.database.codec
        with (
            mock.patch.object(codec, "encode", wraps=codec.encode) as encode,
            mock.patch.object(codec, "decode", wraps=codec.decode) as decode,
        ):
            self._insert_and_check(self._sample_data(), 100)
            results = list(self.database.select("SELECT * FROM $table", Person))
        encode.assert_called_once()
        self.assertIs(encode.call_args.args[0], Person)
        self.assertIs(decode.call_args.args[1], Person)
        self.assertEqual(len(results), 100)
        for instance in results:
            self.assertIs(instance.get_database(), self.database)

    def test_select_rows(self):
        self._insert_all()
        result = self.database.select_rows(
            "SELECT first_name, height, passport FROM $db.person WHERE first_name IN ('Abdul', 'Adena') ORDER BY first_name"
        )
        self.assertEqual(
            result.columns, [("first_name", "String"), ("height", "Float32"), ("passport", "Nullable(UInt32)")]
        )
        rows = list(result)
        self.assertEqual([row[0] for row in rows], ["Abdul", "Adena"])
        self.assertAlmostEqual(rows[0][1], 1.63, places=5)
        self.assertEqual([row[2] for row in rows], [35052255, None])

    def test_select_rows_server_error(self):
        with self.assertRaises(ServerError):
            self.database.select_rows("SELECT * FROM no_such_table")


class Row(Model):
    name = StringField()
    num = Int32Field()

    engine = Memory()


class RowWithFuncDefault(Model):
    num = Int32Field()
    doubled = Int32Field(default=F.multiply(2, 3))

    engine = Memory()


class WithDateTime(Model):
    ts = DateTimeField()

    engine = Memory()
