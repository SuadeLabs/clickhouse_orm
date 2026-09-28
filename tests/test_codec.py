from __future__ import annotations

import datetime
import unittest
from unittest import mock

import pytz

from clickhouse_orm.codec import Codec, TSVCodec
from clickhouse_orm.engines import Memory
from clickhouse_orm.fields import DateTimeField, Int32Field, StringField
from clickhouse_orm.funcs import F
from clickhouse_orm.models import Model

from .base_test_with_data import Person, TestCaseWithData


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
        rows = list(self.codec.decode(lines, Row))
        self.assertEqual([(r.name, r.num) for r in rows], [("a\tb", 1), ("c", 2)])
        self.assertTrue(all(isinstance(r, Row) for r in rows))

    def test_decode_ad_hoc_model(self):
        lines = [b"x\ty", b"UInt8\tString", b"5\thello"]
        (row,) = self.codec.decode(lines)
        self.assertEqual((row.x, row.y), (5, "hello"))
        self.assertNotIsInstance(row, Row)

    def test_decode_timezone(self):
        lines = [b"ts", b"DateTime", b"2020-01-01 12:00:00"]
        (row,) = self.codec.decode(lines, WithDateTime, pytz.timezone("Asia/Jerusalem"))
        self.assertEqual(row.ts, datetime.datetime(2020, 1, 1, 10, 0, tzinfo=pytz.utc))


class DatabaseCodecTestCase(TestCaseWithData):
    def test_default_codec(self):
        self.assertIsInstance(self.database.codec, TSVCodec)

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
