from __future__ import annotations

import datetime
import logging
import unittest
import uuid
from decimal import Decimal
from enum import Enum
from ipaddress import IPv4Address
from unittest import mock

import pytz

from clickhouse_orm.database import ServerError
from clickhouse_orm.driver import RequestsDriver
from clickhouse_orm.fields import ArrayField, DateTimeField, NullableField, StringField, UInt32Field
from clickhouse_orm.funcs import F
from clickhouse_orm.params import EncodedParam, active_params, collect_params, encode_params, format_param
from clickhouse_orm.query import Q, QuerySet

from . import test_funcs, test_mutations, test_querysets
from .base_test_with_data import Person, TestCaseWithData
from .test_queryset_executor import Event, StubExecutor


class Color(Enum):
    red = 1
    green = 2


class FormatParamTestCase(unittest.TestCase):
    def test_scalars(self):
        cases = [
            (None, "\\N"),
            ("a\tb'c\\d\n", "a\\tb\\'c\\\\d\\n"),
            (True, "1"),
            (False, "0"),
            (-5, "-5"),
            (1.5, "1.5"),
            (float("nan"), "nan"),
            (Decimal("1.50"), "1.50"),
            (datetime.date(2020, 6, 11), "2020-06-11"),
            (pytz.utc.localize(datetime.datetime(2020, 6, 11, 9)), "1591866000"),
            (pytz.utc.localize(datetime.datetime(2020, 6, 11, 9, 0, 0, 500000)), "1591866000.500000"),
            # Naive values are wall-clock times, interpreted by the server in the column's timezone
            (datetime.datetime(2020, 6, 11, 9), "2020-06-11 09:00:00"),
            (datetime.datetime(2020, 6, 11, 9, 0, 0, 500000), "2020-06-11 09:00:00.500000"),
            (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
            (IPv4Address("1.2.3.4"), "1.2.3.4"),
            (Color.green, "green"),
            (EncodedParam("raw\\ttext"), "raw\\ttext"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(format_param(value), expected)

    def test_containers(self):
        self.assertEqual(format_param(["a'b", None, "c"]), "['a\\'b',NULL,'c']")
        self.assertEqual(format_param([1, [2, 3]]), "[1,[2,3]]")
        self.assertEqual(format_param(("a", 1, datetime.date(2020, 1, 1))), "('a',1,'2020-01-01')")
        self.assertEqual(format_param({"a": 1, "b": Color.red}), "{'a':1,'b':'red'}")

    def test_unsupported(self):
        with self.assertRaises(TypeError):
            format_param(object())

    def test_encode_params(self):
        self.assertIsNone(encode_params(None))
        self.assertIsNone(encode_params({}))
        self.assertEqual(encode_params({"x": "a", "y": [1]}), {"x": "a", "y": "[1]"})

    def test_encoded_param_for_field(self):
        field = ArrayField(NullableField(DateTimeField()))
        self.assertEqual(
            EncodedParam.for_field(field, ["2020-06-11 09:00:00", None]).text, "['2020-06-11 09:00:00', NULL]"
        )


class CollectParamsTestCase(unittest.TestCase):
    def test_nesting_shares_params(self):
        self.assertIsNone(active_params())
        with collect_params() as outer:
            with collect_params() as inner:
                self.assertIs(inner, outer)
            self.assertIs(active_params(), outer)
        self.assertIsNone(active_params())

    def test_field_param_types(self):
        cases = [
            (NullableField(UInt32Field()), 5, "{_orm_p0:UInt32}"),
            (ArrayField(NullableField(StringField())), ["a"], "{_orm_p0:Array(Nullable(String))}"),
            (
                DateTimeField(timezone="Europe/Madrid"),
                pytz.utc.localize(datetime.datetime(2020, 1, 1)),
                "{_orm_p0:DateTime('Europe/Madrid')}",
            ),
        ]
        for field, value, placeholder in cases:
            with self.subTest(field=field), collect_params() as params:
                self.assertEqual(params.bind_field_value(field, value), placeholder)

    def test_null_is_inlined(self):
        with collect_params() as params:
            self.assertEqual(params.bind_field_value(NullableField(UInt32Field()), None), "\\N")
        self.assertEqual(params.values, {})


class ParameterizedSQLTestCase(unittest.TestCase):
    def setUp(self):
        self.qs = Event.objects_in(StubExecutor())

    def test_as_sql_is_unchanged(self):
        qs = self.qs.filter(name="x").parameterized()
        self.assertEqual(qs.as_sql(), self.qs.filter(name="x").as_sql())
        self.assertEqual(str(qs), qs.as_sql())

    def test_operators(self):
        qs = self.qs.filter(
            Q(name="a'b") | Q(name__in=["x", "y"]),
            value__between=(1, 5),
            date__gt=datetime.date(2020, 1, 1),
            name__iexact="Q",
        )
        sql, params = qs.as_sql_with_params()
        where = sql.split("WHERE ")[1]
        self.assertEqual(
            where,
            "((name = {_orm_p0:String}) OR (name IN ({_orm_p1:String}, {_orm_p2:String})))"
            " AND ((value BETWEEN {_orm_p3:Int32} AND {_orm_p4:Int32})"
            " AND (date > {_orm_p5:Date})"
            " AND (lowerUTF8(name) = lowerUTF8({_orm_p6:String})))",
        )
        self.assertEqual(
            {k: v.text for k, v in params.items()},
            {
                "_orm_p0": "a\\'b",
                "_orm_p1": "x",
                "_orm_p2": "y",
                "_orm_p3": "1",
                "_orm_p4": "5",
                "_orm_p5": "2020-01-01",
                "_orm_p6": "Q",
            },
        )

    def test_like(self):
        sql, params = self.qs.filter(name__contains="5%_\\", name__istartswith="A").as_sql_with_params()
        self.assertIn("name LIKE {_orm_p0:String}", sql)
        self.assertIn("lowerUTF8(name) LIKE lowerUTF8({_orm_p1:String})", sql)
        # LIKE metacharacters are escaped in the pattern, which is then encoded as a parameter
        self.assertEqual(params["_orm_p0"].text, "%5\\\\%\\\\_\\\\\\\\%")
        self.assertEqual(params["_orm_p1"].text, "A%")

    def test_function_arguments(self):
        dt = pytz.utc.localize(datetime.datetime(2020, 6, 11, 9))
        sql, params = self.qs.filter(
            F.equals(Event.name, "x"), Event.date > datetime.date(2020, 1, 1), F.toDate(dt) > Event.date
        ).as_sql_with_params()
        self.assertIn("(`name` = {_orm_p0:String})", sql)
        self.assertIn("(`date` > {_orm_p1:Date})", sql)
        self.assertIn("(toDate({_orm_p2:DateTime}) > `date`)", sql)
        self.assertEqual(params["_orm_p2"].text, "1591866000")

    def test_numbers_and_timezones_stay_inline(self):
        sql, params = self.qs.filter(F.toStartOfHour(F.now(), pytz.utc) > F.toDateTime(3)).as_sql_with_params()
        self.assertIn("toStartOfHour(now(), 'UTC')", sql)
        self.assertIn("toDateTime(3)", sql)
        self.assertEqual(params, {})

    def test_subquery_shares_params(self):
        inner = Event.objects_in(StubExecutor()).filter(value=7).only("name")
        sql, params = self.qs.filter(name="a", name__in=inner).as_sql_with_params()
        self.assertIn("name = {_orm_p0:String}", sql)
        self.assertIn("WHERE value = {_orm_p1:Int32})", sql)
        self.assertEqual(list(params), ["_orm_p0", "_orm_p1"])

    def test_ddl_is_never_parameterized(self):
        with collect_params() as params:
            sql = Event.create_table_sql("db")
        self.assertEqual(params.values, {})
        self.assertEqual(sql, Event.create_table_sql("db"))


class ParameterizedExecutionTestCase(unittest.TestCase):
    def setUp(self):
        self.executor = StubExecutor(raw_result="3", count_result=2)
        self.qs = Event.objects_in(self.executor).filter(name="a").parameterized()

    def test_executor_receives_params(self):
        list(self.qs)
        self.qs.count()
        self.qs.distinct().count()
        self.qs.delete()
        self.qs.update(name="b")
        self.qs.aggregate("name", n="count()").count()
        list(self.qs.aggregate("name", n="count()"))
        self.assertEqual(len(self.executor.calls), 7)

    def test_param_passing(self):
        calls = []
        executor = mock.Mock(wraps=self.executor)
        executor.select.side_effect = lambda *a, **kw: calls.append(kw) or iter([])
        executor.count.side_effect = lambda *a, **kw: calls.append(kw) or 0
        executor.raw.side_effect = lambda *a, **kw: calls.append(kw) or ""
        qs = Event.objects_in(executor).filter(name="a")
        list(qs)
        qs.count()
        self.assertEqual(calls, [{}, {}])  # non-parameterized querysets don't pass params
        qs = qs.parameterized()
        list(qs)
        qs.count()
        qs.update(name="b")
        self.assertEqual([sorted(kw["params"]) for kw in calls[2:]], [["_orm_p0"], ["_orm_p0"], ["_orm_p0", "_orm_p1"]])

    def test_disable(self):
        self.assertFalse(self.qs.parameterized(False)._parameterized)
        self.assertTrue(self.qs.aggregate("name", n="count()")._parameterized)


class RequestsDriverParamsTestCase(unittest.TestCase):
    def test_params_sent_as_url_params(self):
        driver = RequestsDriver("http://example.com:8123/", timeout=5)
        with mock.patch.object(driver.session, "post", return_value=mock.Mock(text="1", status_code=200)) as post:
            driver.scalar("SELECT {x:String}", settings={"database": "db"}, params={"x": "a\\tb"})
        post.assert_called_once_with(
            "http://example.com:8123/",
            params={"database": "db", "param_x": "a\\tb"},
            data=b"SELECT {x:String}",
            stream=False,
            timeout=5,
        )


class DatabaseParamsTestCase(TestCaseWithData):
    def setUp(self):
        super().setUp()
        self._insert_all()

    def test_select_raw_rows_count(self):
        query = "SELECT first_name FROM $table WHERE first_name IN {names:Array(String)} ORDER BY first_name"
        names = [p.first_name for p in self.database.select(query, Person, params={"names": ["Abdul", "Adam"]})]
        self.assertEqual(names, ["Abdul", "Adam"])
        self.assertEqual(self.database.raw("SELECT {x:String}", params={"x": "a\tb"}), "a\\tb\n")
        result = self.database.select_rows(
            "SELECT {d:Date}, {n:Nullable(Int32)}", params={"d": "2020-01-01", "n": None}
        )
        self.assertEqual(list(result), [(datetime.date(2020, 1, 1), None)])
        self.assertEqual(
            self.database.count(Person, "height > {h:Float64}", params={"h": 1.7}),
            self.database.count(Person, "height > 1.7"),
        )
        page = self.database.paginate(Person, "first_name", 1, 5, "first_name > {n:String}", params={"n": "B"})
        expected = self.database.paginate(Person, "first_name", 1, 5, "first_name > 'B'")
        self.assertEqual([p.to_dict() for p in page.objects], [p.to_dict() for p in expected.objects])
        self.assertEqual(page.number_of_objects, expected.number_of_objects)

    def test_missing_param(self):
        with self.assertRaises(ServerError):
            self.database.raw("SELECT {x:String}")

    def test_injection_attempt_is_data(self):
        evil = "x' OR 1=1 OR first_name = '"
        qs = Person.objects_in(self.database).parameterized()
        self.assertEqual(qs.filter(first_name=evil).count(), 0)
        self.assertEqual(list(qs.filter(first_name__contains=evil)), [])
        self.assertEqual(qs.filter(Person.first_name == evil).count(), 0)

    def test_values_round_trip(self):
        odd = "tab\tnewline\nquote'backslash\\é%_"
        self.database.insert([Person(first_name=odd, last_name="L", birthday="2000-01-01", height=1)])
        qs = Person.objects_in(self.database).parameterized()
        for condition in (Q(first_name=odd), Q(first_name__contains=odd[3:-3]), Q(first_name__iendswith="É%_")):
            with self.subTest(condition=condition):
                self.assertEqual([p.first_name for p in qs.filter(condition)], [odd])
        self.assertEqual(qs.filter(first_name__in=[odd, "Abdul"]).count(), 2)
        nulls = Person.objects_in(self.database).filter(passport=None).count()
        self.assertGreater(nulls, 0)
        self.assertEqual(qs.filter(passport=None).count(), nulls)

    def test_values_are_sent_as_params(self):
        qs = Person.objects_in(self.database).filter(first_name="Abdul").parameterized()
        with mock.patch.object(self.database.driver, "send", wraps=self.database.driver.send) as send:
            self.assertEqual(len(list(qs)), 1)
        (call,) = send.call_args_list
        self.assertNotIn("Abdul", call.args[0])
        self.assertEqual(call.kwargs["params"], {"_orm_p0": "Abdul"})


class _ForceParameterized:
    """Re-runs an existing test case with every queryset parameterized."""

    def setUp(self):
        patcher = mock.patch.object(QuerySet, "_parameterized", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()


class ParameterizedQuerySetTestCase(_ForceParameterized, test_querysets.QuerySetTestCase):
    pass


class ParameterizedAggregateTestCase(_ForceParameterized, test_querysets.AggregateTestCase):
    pass


class ParameterizedMutationsTestCase(_ForceParameterized, test_mutations.MutationsTestCase):
    pass


class ParameterizedFuncsTestCase(_ForceParameterized, test_funcs.FuncsTestCase):
    def _call_func(self, func):
        with collect_params() as params:
            sql = "SELECT %s AS value" % func.to_sql()
        logging.info("%s %s", sql, params.values)
        try:
            result = list(self.database.select(sql, params=params.values))
            return result[0].value if result else None
        except ServerError as e:
            if "Unknown function" in str(e):
                return
            raise
