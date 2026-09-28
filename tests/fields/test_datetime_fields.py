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

        # Columns without a timezone are loaded as naive (wall-clock) datetimes, as they were inserted
        query = "SELECT toStartOfHour(datetime_field) as hour_start, * from $db.modelwithdate ORDER BY date_field"
        results = list(self.database.select(query))
        self.assertEqual(len(results), 2)
        # Older servers type toStartOfHour's result with the server's timezone, e.g. DateTime('UTC')
        hour_starts = []
        for result in results:
            if result.hour_start.tzinfo:
                self.assertEqual(result.hour_start.tzinfo.zone, self.database.server_timezone.zone)
            hour_starts.append(result.hour_start.replace(tzinfo=None))
        self.assertEqual(
            hour_starts, [datetime.datetime(2016, 8, 30, 3, 0, 0), datetime.datetime(2016, 8, 31, 1, 0, 0)]
        )
        self.assertEqual(results[0].date_field, datetime.date(2016, 8, 30))
        self.assertEqual(results[0].datetime_field, datetime.datetime(2016, 8, 30, 3, 50, 0))
        self.assertEqual(results[1].date_field, datetime.date(2016, 8, 31))
        self.assertEqual(results[1].datetime_field, datetime.datetime(2016, 8, 31, 1, 30, 0))
        self.assertEqual(results[0].datetime64_field, datetime.datetime(2016, 8, 30, 3, 50, 0, 123456))
        self.assertEqual(results[0].datetime64_3_field, datetime.datetime(2016, 8, 30, 3, 50, 0, 123000))
        self.assertEqual(results[1].datetime64_field, datetime.datetime(2016, 8, 31, 1, 30, 0, 123456))
        self.assertEqual(results[1].datetime64_3_field, datetime.datetime(2016, 8, 31, 1, 30, 0, 123000))

    def test_naive_values_are_wall_clock_times(self):
        # Naive values are interpreted in the server's timezone, whatever it is
        naive = datetime.datetime(2020, 6, 11, 4, 0, 0, 123456)
        self.database.insert([ModelWithDate(date_field="2020-06-11", datetime_field=naive, datetime64_field=naive)])
        query = "SELECT toString(datetime_field) AS s, toString(datetime64_field) AS s64 FROM $db.modelwithdate"
        self.assertEqual(
            list(self.database.select_rows(query)), [("2020-06-11 04:00:00", "2020-06-11 04:00:00.123456")]
        )
        qs = ModelWithDate.objects_in(self.database)
        for parameterized in (False, True):
            self.assertEqual(
                qs.filter(datetime_field=naive.replace(microsecond=0)).parameterized(parameterized).count(), 1
            )
            self.assertEqual(qs.filter(datetime64_field=naive).parameterized(parameterized).count(), 1)
        count = "SELECT count() FROM $db.modelwithdate WHERE datetime_field = {dt:DateTime}"
        self.assertEqual(self.database.raw(count, params={"dt": naive.replace(microsecond=0)}).strip(), "1")

    def test_aware_values_are_instants(self):
        aware = pytz.timezone("Asia/Tokyo").localize(datetime.datetime(2020, 6, 11, 13, 0, 0))
        self.database.insert([ModelWithDate(date_field="2020-06-11", datetime_field=aware, datetime64_field=aware)])
        query = "SELECT toUnixTimestamp(datetime_field), toUnixTimestamp(datetime64_field) FROM $db.modelwithdate"
        self.assertEqual(list(self.database.select_rows(query)), [(int(aware.timestamp()),) * 2])
        (instance,) = self.database.select("SELECT * FROM $db.modelwithdate", ModelWithDate)
        # The value is loaded as a naive wall-clock time in the server's timezone
        expected = aware.astimezone(self.database.server_timezone).replace(tzinfo=None)
        self.assertEqual(instance.datetime_field, expected)


class DateTimeToPythonTest(unittest.TestCase):
    def setUp(self):
        self.field = DateTimeField()
        self.tz = pytz.timezone("Asia/Jerusalem")
        self.tz_field = DateTimeField(timezone=self.tz)
        self.naive = datetime.datetime(2020, 6, 11, 4, 0)

    def test_naive_values_are_kept_naive(self):
        for value in (self.naive, "2020-06-11 04:00:00"):
            # The timezone argument is ignored
            for timezone in (None, pytz.utc, self.tz):
                self.assertEqual(self.field.to_python(value, timezone), self.naive)
                self.assertIsNone(self.field.to_python(value, timezone).tzinfo)
        self.assertEqual(self.field.to_python(datetime.date(2020, 6, 11), pytz.utc), datetime.datetime(2020, 6, 11))

    def test_naive_values_are_localized_to_field_timezone(self):
        expected = self.tz.localize(self.naive)
        for value in (self.naive, "2020-06-11 04:00:00"):
            result = self.tz_field.to_python(value, pytz.utc)
            self.assertEqual(result, expected)
            self.assertEqual(result.tzinfo.zone, "Asia/Jerusalem")
        self.assertEqual(
            self.tz_field.to_python(datetime.date(2020, 6, 11), None), self.tz.localize(datetime.datetime(2020, 6, 11))
        )

    def test_absolute_values(self):
        aware = pytz.utc.localize(self.naive)
        for field in (self.field, self.tz_field):
            self.assertIs(field.to_python(aware, self.tz), aware)
            self.assertEqual(field.to_python(1591848000, self.tz), aware)
            self.assertEqual(field.to_python("1591848000", self.tz), aware)
            self.assertEqual(field.to_python("2020-06-11 07:00:00+03:00", None), aware)
        self.assertEqual(DateTime64Field().to_python(1591848000.5, None), aware.replace(microsecond=500000))

    def test_assignment(self):
        instance = ModelWithDate(datetime_field=self.naive, datetime64_field="2020-06-11")
        self.assertEqual(instance.datetime_field, self.naive)
        self.assertEqual(instance.datetime64_field, datetime.datetime(2020, 6, 11))
        instance = ModelWithTz(datetime_tz_field=self.naive)
        self.assertEqual(instance.datetime_tz_field, pytz.timezone("Europe/Madrid").localize(self.naive))

    def test_to_db_string(self):
        naive = self.naive.replace(microsecond=123456)
        aware = pytz.utc.localize(naive)
        self.assertEqual(self.field.to_db_string(naive), "'2020-06-11 04:00:00'")
        self.assertEqual(self.field.to_db_string(aware, quote=False), "1591848000")
        self.assertEqual(DateTime64Field().to_db_string(naive), "'2020-06-11 04:00:00.123456'")
        self.assertEqual(DateTime64Field(precision=3).to_db_string(naive), "'2020-06-11 04:00:00.123'")
        self.assertEqual(DateTime64Field(precision=0).to_db_string(naive), "'2020-06-11 04:00:00'")
        self.assertEqual(DateTime64Field(precision=3).to_db_string(aware, quote=False), "1591848000.123")


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
        madrid = pytz.timezone("Europe/Madrid")
        self.database.insert(
            [
                ModelWithTz(
                    datetime_no_tz_field="2020-06-11 04:00:00",
                    datetime_tz_field="2020-06-11 04:00:00",
                    datetime64_tz_field="2020-06-11 04:00:00",
                    datetime_utc_field="2020-06-11 04:00:00",
                ),
                ModelWithTz(
                    datetime_no_tz_field="2020-06-11 08:00:00+0300",
                    datetime_tz_field="2020-06-11 08:00:00+0300",
                    datetime64_tz_field="2020-06-11 08:00:00+0300",
                    datetime_utc_field="2020-06-11 08:00:00+0300",
                ),
            ]
        )
        query = "SELECT * from $db.modelwithtz ORDER BY datetime_utc_field"
        for model_class in (ModelWithTz, None):
            results = list(self.database.select(query, model_class))
            # Naive values are wall-clock times in the column's timezone
            self.assertEqual(results[0].datetime_no_tz_field, datetime.datetime(2020, 6, 11, 4, 0))
            self.assertEqual(results[0].datetime_tz_field, madrid.localize(datetime.datetime(2020, 6, 11, 4, 0)))
            self.assertEqual(results[0].datetime64_tz_field, madrid.localize(datetime.datetime(2020, 6, 11, 4, 0)))
            self.assertEqual(results[0].datetime_utc_field, datetime.datetime(2020, 6, 11, 4, 0, tzinfo=pytz.UTC))
            # Aware values are instants
            instant = datetime.datetime(2020, 6, 11, 5, 0, tzinfo=pytz.UTC)
            server_time = instant.astimezone(self.database.server_timezone).replace(tzinfo=None)
            self.assertEqual(results[1].datetime_no_tz_field, server_time)
            self.assertEqual(results[1].datetime_tz_field, instant)
            self.assertEqual(results[1].datetime64_tz_field, instant)
            self.assertEqual(results[1].datetime_utc_field, instant)
            for result in results:
                self.assertIsNone(result.datetime_no_tz_field.tzinfo)
                self.assertEqual(result.datetime_tz_field.tzinfo.zone, "Europe/Madrid")
                self.assertEqual(result.datetime64_tz_field.tzinfo.zone, "Europe/Madrid")
                self.assertEqual(result.datetime_utc_field.tzinfo.zone, "UTC")

    def test_column_timezone(self):
        # The column's type determines whether values are naive, whatever the model's field
        self.database.insert([ModelWithTz(datetime_no_tz_field="2020-06-11 04:00:00+00:00")])
        query = "SELECT toDateTime(datetime_utc_field, 'Asia/Tokyo') AS datetime_no_tz_field FROM $db.modelwithtz"
        (result,) = self.database.select(query, ModelWithTz)
        self.assertEqual(result.datetime_no_tz_field, pytz.utc.localize(datetime.datetime(1970, 1, 1)))
        self.assertEqual(result.datetime_no_tz_field.tzinfo.zone, "Asia/Tokyo")
