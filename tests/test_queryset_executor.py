from __future__ import annotations

import unittest
from copy import deepcopy

from clickhouse_orm.database import Database
from clickhouse_orm.engines import ReplacingMergeTree
from clickhouse_orm.executor import Executor
from clickhouse_orm.fields import DateField, Int32Field, StringField
from clickhouse_orm.funcs import F
from clickhouse_orm.models import Model
from clickhouse_orm.query import AggregateQuerySet, Q, QuerySet


class StubExecutor:
    """Records the SQL it receives and returns canned results."""

    def __init__(self, rows=(), raw_result="", count_result=0):
        self.rows = list(rows)
        self.raw_result = raw_result
        self.count_result = count_result
        self.calls = []
        self.params = []

    def select(self, query, model_class=None, settings=None, params=None):
        self.params.append(params)
        self.calls.append(("select", query, model_class))
        return iter(self.rows)

    def raw(self, query, settings=None, stream=False, params=None):
        self.params.append(params)
        self.calls.append(("raw", query))
        return self.raw_result

    def count(self, model_class, conditions=None, params=None):
        self.params.append(params)
        self.calls.append(("count", model_class, conditions))
        return self.count_result


class Event(Model):
    date = DateField()
    name = StringField()
    value = Int32Field()

    engine = ReplacingMergeTree("date", ("date", "name"))


class ExecutorProtocolTestCase(unittest.TestCase):
    def test_implementations(self):
        self.assertIsInstance(StubExecutor(), Executor)
        self.assertTrue(issubclass(Database, Executor))
        self.assertNotIsInstance(object(), Executor)

    def test_objects_in_accepts_any_executor(self):
        executor = StubExecutor()
        qs = Event.objects_in(executor)
        self.assertIsInstance(qs, QuerySet)
        self.assertIs(qs._executor, executor)
        self.assertIs(qs._database, executor)


class QuerySetWithExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.executor = StubExecutor()
        self.qs = Event.objects_in(self.executor)

    def test_building_does_not_execute(self):
        qs = self.qs.filter(name="a").exclude(value__gt=3).order_by("-date").only("name")[10:20]
        str(qs)
        qs.aggregate("name", total="sum(value)").with_totals().as_sql()
        self.assertEqual(self.executor.calls, [])

    def test_as_sql(self):
        qs = self.qs.filter(Q(name="a") | Q(value__gte=3), prewhere=True).filter(date="2020-01-01").distinct()
        self.assertEqual(
            qs.order_by("-date").only("name")[5:15].as_sql(),
            "SELECT DISTINCT `name`\nFROM `event`\n"
            "PREWHERE (name = 'a') OR (value >= 3)\nWHERE date = '2020-01-01'\n"
            "ORDER BY date DESC\nLIMIT 5, 10",
        )

    def test_iteration_uses_select(self):
        self.executor.rows = [Event(name="a"), Event(name="b")]
        self.assertEqual([e.name for e in self.qs.filter(value=1)], ["a", "b"])
        self.assertEqual(
            self.executor.calls,
            [("select", "SELECT `date`, `name`, `value`\nFROM `event`\nWHERE value = 1", Event)],
        )

    def test_indexing(self):
        self.executor.rows = [Event(name="a")]
        self.assertEqual(self.qs[3].name, "a")
        self.assertEqual(self.executor.calls[0][1].splitlines()[-1], "LIMIT 3, 1")

    def test_simple_count(self):
        self.executor.count_result = 7
        self.assertEqual(self.qs.filter(name="a").count(), 7)
        self.assertEqual(self.executor.calls, [("count", Event, "name = 'a'")])

    def test_truthiness(self):
        self.assertFalse(self.qs)
        self.executor.count_result = 1
        self.assertTrue(self.qs)

    def test_count_with_subquery(self):
        self.executor.raw_result = "4\n"
        self.assertEqual(self.qs.distinct().count(), 4)
        self.executor.raw_result = ""
        self.assertEqual(self.qs[:5].count(), 0)
        self.assertEqual(
            [call[1] for call in self.executor.calls],
            [
                "SELECT count() FROM (SELECT DISTINCT `date`, `name`, `value`\nFROM `event`)",
                "SELECT count() FROM (SELECT `date`, `name`, `value`\nFROM `event`\nLIMIT 0, 5)",
            ],
        )

    def test_deepcopy_shares_executor(self):
        qs = self.qs.filter(name="a").order_by("date")
        copied = deepcopy(qs)
        self.assertIsNot(copied, qs)
        self.assertIs(copied._executor, self.executor)
        self.assertEqual(copied.as_sql(), qs.as_sql())
        # Querysets used as filter values are copied along with the conditions
        outer = self.qs.filter(name__in=qs.only("name"))
        self.assertEqual(outer.filter(value=1).as_sql().count("`event`"), 2)

    def test_paginate(self):
        self.executor.count_result = 25
        self.executor.rows = [Event()] * 10
        page = self.qs.order_by("date").paginate(page_num=-1, page_size=10)
        self.assertEqual((page.number, page.pages_total, page.number_of_objects), (3, 3, 25))
        self.assertEqual(self.executor.calls[-1][1].splitlines()[-1], "LIMIT 20, 10")

    def test_delete_and_update(self):
        self.qs.filter(name="a").delete()
        self.qs.filter(value__lt=0).update(value=F.abs(Event.value), name="x")
        self.assertEqual(
            self.executor.calls,
            [
                ("raw", "ALTER TABLE $db.`event` DELETE WHERE name = 'a'"),
                ("raw", "ALTER TABLE $db.`event` UPDATE `value` = abs(`value`), `name` = 'x' WHERE value < 0"),
            ],
        )

    def test_final(self):
        self.assertEqual(self.qs.final().as_sql(), "SELECT `date`, `name`, `value`\nFROM `event` FINAL")


class AggregateQuerySetWithExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.executor = StubExecutor(raw_result="2")
        self.qs = Event.objects_in(self.executor).filter(value__gt=0).aggregate("name", total="sum(value)")

    def test_inherits_executor(self):
        self.assertIsInstance(self.qs, AggregateQuerySet)
        self.assertIs(self.qs._executor, self.executor)

    def test_iteration_uses_ad_hoc_select(self):
        list(self.qs.with_totals())
        self.assertEqual(
            self.executor.calls,
            [
                (
                    "select",
                    "SELECT name, sum(value) AS total\nFROM `event`\nWHERE value > 0\nGROUP BY `name` WITH TOTALS",
                    None,
                )
            ],
        )

    def test_count(self):
        self.assertEqual(self.qs.count(), 2)
        self.assertEqual(
            self.executor.calls,
            [
                (
                    "raw",
                    "SELECT count() FROM (SELECT name, sum(value) AS total\nFROM `event`\nWHERE value > 0\nGROUP BY `name`)",
                )
            ],
        )
