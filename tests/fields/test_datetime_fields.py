from __future__ import annotations

import datetime
import unittest

import pytz

from clickhouse_orm.database import Database
from clickhouse_orm.engines import MergeTree
from clickhouse_orm.fields import DateField, DateTime64Field, DateTimeField
from clickhouse_orm.models import Model


class DateFieldsTest(unittest.TestCase):
    def setUp(self):
        self.database = Database("test-db", log_statements=True)
        if self.database.server_version < (20, 1, 2, 4):
            raise unittest.SkipTest("ClickHouse version too old")
        self.database.create_table(ModelWithDate)

    def tearDown(self):
        self.database.drop_database()

    def test_ad_hoc_model(self):
        self.database.insert(
            [
                ModelWithDate(
                    date_field="2016-08-30",
                    datetime_field="2016-08-30 03:50:00",
                    datetime64_field="2016-08-30 03:50:00.123456",
                    datetime64_3_field="2016-08-30 03:50:00.123456",
                ),
                ModelWithDate(
                    date_field="2016-08-31",
                    datetime_field="2016-08-31 01:30:00",
                    datetime64_field="2016-08-31 01:30:00.123456",
                    datetime64_3_field="2016-08-31 01:30:00.123456",
                ),
            ]
        )

        # toStartOfHour returns DateTime('Asia/Yekaterinburg') in my case, so I test it here to
        query = "SELECT toStartOfHour(datetime_field) as hour_start, * from $db.modelwithdate ORDER BY date_field"
        results = list(self.database.select(query))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].date_field, datetime.date(2016, 8, 30))
        self.assertEqual(
            results[0].datetime_field,
            datetime.datetime(2016, 8, 30, 3, 50, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[0].hour_start,
            datetime.datetime(2016, 8, 30, 3, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(results[1].date_field, datetime.date(2016, 8, 31))
        self.assertEqual(
            results[1].datetime_field,
            datetime.datetime(2016, 8, 31, 1, 30, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].hour_start,
            datetime.datetime(2016, 8, 31, 1, 0, 0, tzinfo=pytz.UTC),
        )

        self.assertEqual(
            results[0].datetime64_field,
            datetime.datetime(2016, 8, 30, 3, 50, 0, 123456, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[0].datetime64_3_field,
            datetime.datetime(2016, 8, 30, 3, 50, 0, 123000, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].datetime64_field,
            datetime.datetime(2016, 8, 31, 1, 30, 0, 123456, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].datetime64_3_field,
            datetime.datetime(2016, 8, 31, 1, 30, 0, 123000, tzinfo=pytz.UTC),
        )


class DateTimeToPythonTest(unittest.TestCase):
    def setUp(self):
        self.field = DateTimeField()
        self.tz = pytz.timezone("Asia/Jerusalem")
        self.naive = datetime.datetime(2020, 6, 11, 4, 0)

    def test_naive_values_are_localized(self):
        expected = self.tz.localize(self.naive)
        for value in (self.naive, "2020-06-11 04:00:00"):
            result = self.field.to_python(value, self.tz)
            self.assertEqual(result, expected)
            self.assertEqual(result.tzinfo.zone, "Asia/Jerusalem")
        self.assertEqual(
            self.field.to_python(datetime.date(2020, 6, 11), self.tz), self.tz.localize(datetime.datetime(2020, 6, 11))
        )

    def test_no_timezone_keeps_naive(self):
        self.assertEqual(self.field.to_python(self.naive, None), self.naive)
        self.assertEqual(self.field.to_python("2020-06-11 04:00:00", None), self.naive)

    def test_absolute_values(self):
        aware = pytz.utc.localize(self.naive)
        self.assertIs(self.field.to_python(aware, self.tz), aware)
        self.assertEqual(self.field.to_python(1591848000, self.tz), aware)
        self.assertEqual(self.field.to_python("2020-06-11 07:00:00+03:00", None), aware)

    def test_assignment_assumes_utc(self):
        instance = ModelWithDate(datetime_field=self.naive, datetime64_field="2020-06-11")
        self.assertEqual(instance.datetime_field, pytz.utc.localize(self.naive))
        self.assertEqual(instance.datetime64_field, pytz.utc.localize(datetime.datetime(2020, 6, 11)))


class ModelWithDate(Model):
    date_field = DateField()
    datetime_field = DateTimeField()
    datetime64_field = DateTime64Field()
    datetime64_3_field = DateTime64Field(precision=3)

    engine = MergeTree("date_field", ("date_field",))


class ModelWithTz(Model):
    datetime_no_tz_field = DateTimeField()  # server tz
    datetime_tz_field = DateTimeField(timezone="Europe/Madrid")
    datetime64_tz_field = DateTime64Field(timezone="Europe/Madrid")
    datetime_utc_field = DateTimeField(timezone=pytz.UTC)

    engine = MergeTree("datetime_no_tz_field", ("datetime_no_tz_field",))


class DateTimeFieldWithTzTest(unittest.TestCase):
    def setUp(self):
        self.database = Database("test-db", log_statements=True)
        if self.database.server_version < (20, 1, 2, 4):
            raise unittest.SkipTest("ClickHouse version too old")
        self.database.create_table(ModelWithTz)

    def tearDown(self):
        self.database.drop_database()

    def test_ad_hoc_model(self):
        self.database.insert(
            [
                ModelWithTz(
                    datetime_no_tz_field="2020-06-11 04:00:00",
                    datetime_tz_field="2020-06-11 04:00:00",
                    datetime64_tz_field="2020-06-11 04:00:00",
                    datetime_utc_field="2020-06-11 04:00:00",
                ),
                ModelWithTz(
                    datetime_no_tz_field="2020-06-11 07:00:00+0300",
                    datetime_tz_field="2020-06-11 07:00:00+0300",
                    datetime64_tz_field="2020-06-11 07:00:00+0300",
                    datetime_utc_field="2020-06-11 07:00:00+0300",
                ),
            ]
        )
        query = "SELECT * from $db.modelwithtz ORDER BY datetime_no_tz_field"
        results = list(self.database.select(query))

        self.assertEqual(
            results[0].datetime_no_tz_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[0].datetime_tz_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[0].datetime64_tz_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[0].datetime_utc_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].datetime_no_tz_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].datetime_tz_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].datetime64_tz_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )
        self.assertEqual(
            results[1].datetime_utc_field,
            datetime.datetime(2020, 6, 11, 4, 0, 0, tzinfo=pytz.UTC),
        )

        self.assertEqual(
            results[0].datetime_no_tz_field.tzinfo.zone,
            self.database.server_timezone.zone,
        )
        self.assertEqual(
            results[0].datetime_tz_field.tzinfo.zone,
            pytz.timezone("Europe/Madrid").zone,
        )
        self.assertEqual(
            results[0].datetime64_tz_field.tzinfo.zone,
            pytz.timezone("Europe/Madrid").zone,
        )
        self.assertEqual(results[0].datetime_utc_field.tzinfo.zone, pytz.timezone("UTC").zone)
        self.assertEqual(
            results[1].datetime_no_tz_field.tzinfo.zone,
            self.database.server_timezone.zone,
        )
        self.assertEqual(
            results[1].datetime_tz_field.tzinfo.zone,
            pytz.timezone("Europe/Madrid").zone,
        )
        self.assertEqual(
            results[1].datetime64_tz_field.tzinfo.zone,
            pytz.timezone("Europe/Madrid").zone,
        )
        self.assertEqual(results[1].datetime_utc_field.tzinfo.zone, pytz.timezone("UTC").zone)
