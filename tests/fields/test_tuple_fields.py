from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

import pytz

from clickhouse_orm.database import Database
from clickhouse_orm.engines import Memory
from clickhouse_orm.fields import (
    ArrayField,
    DateField,
    DateTimeField,
    DecimalField,
    Enum8Field,
    Float32Field,
    Int32Field,
    NullableField,
    StringField,
    TupleField,
    UInt8Field,
)
from clickhouse_orm.models import Model, ModelBase
from clickhouse_orm.utils import parse_array, parse_tuple_type

from .test_array_fields import TRICKY_STRINGS


class Colour(Enum):
    red = 1
    green = 2


class ModelWithTuples(Model):
    id = UInt8Field()
    pair = TupleField([Int32Field(), StringField()])
    named = TupleField([("colour", NullableField(Enum8Field(Colour))), ("when", DateTimeField())])
    single = TupleField([StringField()])
    nested = TupleField([ArrayField(NullableField(StringField())), TupleField([DateField(), Float32Field()])])
    # Unnamed, since older servers (e.g. 21.3) flatten arrays of named tuples into Nested columns
    pairs = ArrayField(TupleField([StringField(), DecimalField(9, 2)]))

    engine = Memory()


def _enum_names(value):
    return tuple(item.name if isinstance(item, Enum) else item for item in value)


class TupleFieldsTest(unittest.TestCase):
    def setUp(self):
        self.database = Database("test-db", log_statements=True)
        self.database.create_table(ModelWithTuples)

    def tearDown(self):
        self.database.drop_database()

    def _instances(self):
        return [
            ModelWithTuples(
                id=1,
                pair=(-1, "a'b"),
                named={"colour": Colour.green, "when": datetime(2020, 1, 2, 3, 4, 5)},
                single=("x",),
                nested=(["a", None, "(,)"], (date(2020, 1, 1), 1.5)),
                pairs=[("k'1", "1.5"), ("[k2]", 2)],
            ),
            ModelWithTuples(id=2),
        ]

    def test_insert_and_select(self):
        instances = self._instances()
        self.database.insert(instances)
        expected = [instance.to_dict() for instance in instances]
        # The default datetime (the epoch) is aware, but is read back as naive since the column has no timezone
        epoch = DateTimeField.class_default.astimezone(self.database.server_timezone).replace(tzinfo=None)
        expected[1]["named"] = (None, epoch)
        query = "SELECT * FROM $db.modelwithtuples ORDER BY id"
        for model_cls in (ModelWithTuples, None):
            results = list(self.database.select(query, model_cls))
            # Ad-hoc models have their own enum classes
            actual = [dict(result.to_dict(), named=_enum_names(result.named)) for result in results]
            self.assertEqual(actual, [dict(e, named=_enum_names(e["named"])) for e in expected])
        self.assertEqual(expected[0]["named"], (Colour.green, datetime(2020, 1, 2, 3, 4, 5)))
        self.assertEqual(expected[0]["pairs"], [("k'1", Decimal("1.50")), ("[k2]", Decimal("2.00"))])
        self.assertEqual(expected[1]["nested"], ([], (DateField.class_default, 0.0)))

    def test_select_rows(self):
        self.database.insert(self._instances()[:1])
        (row,) = self.database.select_rows("SELECT pair, named, single, nested, pairs FROM $db.modelwithtuples")
        self.assertEqual(
            row,
            (
                (-1, "a'b"),
                ("green", datetime(2020, 1, 2, 3, 4, 5)),
                ("x",),
                (["a", None, "(,)"], (date(2020, 1, 1), 1.5)),
                [("k'1", Decimal("1.5")), ("[k2]", Decimal("2"))],
            ),
        )

    def test_strings_with_special_characters(self):
        instances = [ModelWithTuples(id=i, pair=(i, s), single=(s,)) for i, s in enumerate(TRICKY_STRINGS)]
        self.database.insert(instances)
        results = list(ModelWithTuples.objects_in(self.database).order_by("id"))
        self.assertEqual([r.pair[1] for r in results], TRICKY_STRINGS)
        self.assertEqual([r.single for r in results], [(s,) for s in TRICKY_STRINGS])
        # The values can be compared in queries too
        for parameterized in (False, True):
            for s in TRICKY_STRINGS:
                qs = ModelWithTuples.objects_in(self.database).filter(single=(s,), pair=(TRICKY_STRINGS.index(s), s))
                self.assertEqual(qs.parameterized(parameterized).count(), 1, s)

    def test_filters(self):
        self.database.insert(self._instances())
        instance = self._instances()[0]
        for parameterized in (False, True):
            qs = ModelWithTuples.objects_in(self.database).parameterized(parameterized)
            for name in ("pair", "named", "single", "nested"):
                self.assertEqual(qs.filter(**{name: getattr(instance, name)}).count(), 1, name)
            self.assertEqual(qs.filter(pair__in=[(-1, "a'b"), (5, "x")]).count(), 1)
            self.assertEqual(qs.exclude(single=("x",)).count(), 1)

    def test_params(self):
        self.database.insert(self._instances())
        query = "SELECT count() FROM $db.modelwithtuples WHERE pair = {p:Tuple(Int32, String)}"
        self.assertEqual(self.database.raw(query, params={"p": (-1, "a'b")}).strip(), "1")

    def test_ddl(self):
        sql = ModelWithTuples.create_table_sql(self.database.db_name)
        self.assertIn("pair Tuple(Int32, String) DEFAULT (0,'')", sql)
        self.assertIn(
            "named Tuple(colour Nullable(Enum8('red' = 1, 'green' = 2)), when DateTime) DEFAULT (NULL,'0000000000')",
            sql,
        )
        self.assertIn("single Tuple(String) DEFAULT ('',)", sql)
        self.assertIn("pairs Array(Tuple(String, Decimal(9,2)))", sql)
        # The table matches the model
        model = self.database.get_model_for_table("modelwithtuples")
        for name, field in ModelWithTuples.fields().items():
            self.assertEqual(model.fields()[name].get_sql(False), field.get_sql(False))


class TupleFieldUnitTest(unittest.TestCase):
    def test_to_python(self):
        field = TupleField([Int32Field(), NullableField(StringField()), ArrayField(UInt8Field())])
        for value in ((1, "a", [2]), [1, "a", [2]], "(1,'a',[2])", b"(1, 'a', [2])", ("1", "a", ["2"])):
            self.assertEqual(field.to_python(value, pytz.utc), (1, "a", [2]))
        self.assertEqual(field.to_python("(1,NULL,[])", pytz.utc), (1, None, []))
        for value in ((1, "a"), (1, "a", [2], 3), "1,'a',[2]", "[1,'a',[2]]", 5, {"a": 1}, "(1,'a',[2]"):
            with self.assertRaises(ValueError):
                field.to_python(value, pytz.utc)

    def test_named(self):
        field = TupleField([("a", Int32Field()), ("b c", StringField())])
        self.assertEqual(field.names, ["a", "b c"])
        self.assertEqual(field.to_python({"b c": "x", "a": "1"}, pytz.utc), (1, "x"))
        self.assertEqual(field.to_python((1, "x"), pytz.utc), (1, "x"))
        with self.assertRaises(ValueError):
            field.to_python({"a": 1}, pytz.utc)
        self.assertEqual(field.get_sql(), "Tuple(a Int32, `b c` String) DEFAULT (0,'')")

    def test_to_db_string(self):
        field = TupleField([NullableField(StringField()), DateField(), ArrayField(Int32Field())])
        self.assertEqual(field.to_db_string(("a'b\t", date(2020, 1, 1), [1, 2])), "('a\\'b\\t','2020-01-01',[1, 2])")
        self.assertEqual(field.to_db_string((None, date(2020, 1, 1), []), quote=False), "(NULL,'2020-01-01',[])")
        # Single-element tuples need a trailing comma in SQL
        self.assertEqual(TupleField([StringField()]).to_db_string(("a",)), "('a',)")
        self.assertEqual(TupleField([]).to_db_string(()), "()")

    def test_default(self):
        field = TupleField([Int32Field(default=5), StringField()])
        self.assertEqual(field.default, (5, ""))
        self.assertEqual(TupleField([Int32Field()], default=(3,)).default, (3,))
        self.assertEqual(ModelWithTuples().pair, (0, ""))

    def test_validate(self):
        field = TupleField([UInt8Field(), StringField()])
        field.validate((1, "a"))
        with self.assertRaises(ValueError):
            field.validate((300, "a"))

    def test_param_type(self):
        field = TupleField([("a", NullableField(Int32Field())), ("b", ArrayField(NullableField(StringField())))])
        self.assertEqual(field._param_type(), "Tuple(a Nullable(Int32), b Array(Nullable(String)))")

    def test_invalid_inner_fields(self):
        for inner_fields in ([DateField], [None], [("a", Int32Field()), Int32Field()], [("a", Int32Field())] * 2):
            with self.assertRaises(AssertionError):
                TupleField(inner_fields)

    def test_create_ad_hoc_field(self):
        cases = [
            "Tuple(UInt64, UInt64, UUID)",
            "Tuple(a Nullable(String), `b c` Array(DateTime('Asia/Tokyo')), d Tuple(Decimal(9, 2), Enum8('x y' = 1)))",
            "Array(Tuple(name String, type String))",
            "Tuple(UInt8)",
        ]
        for db_type in cases:
            field = ModelBase.create_ad_hoc_field(db_type)
            self.assertEqual(field.get_sql(False).replace(", ", ","), db_type.replace(", ", ","))
        field = ModelBase.create_ad_hoc_field(cases[1])
        self.assertEqual(field.names, ["a", "b c", "d"])
        self.assertEqual(field.inner_fields[1].inner_field.timezone.zone, "Asia/Tokyo")

    def test_parse_tuple_type(self):
        self.assertEqual(
            parse_tuple_type("Tuple(a UInt8, `b c` Decimal(9, 2), `d` Enum8('x, y' = 1))"),
            [("a", "UInt8"), ("b c", "Decimal(9, 2)"), ("d", "Enum8('x, y' = 1)")],
        )
        self.assertEqual(
            parse_tuple_type("Tuple(DateTime('UTC'), Nullable(String), Tuple(x UInt8))"),
            [(None, "DateTime('UTC')"), (None, "Nullable(String)"), (None, "Tuple(x UInt8)")],
        )
        self.assertEqual(parse_tuple_type("Tuple()"), [])

    def test_parse_array(self):
        self.assertEqual(parse_array("(1,'a\\'b',NULL)"), ["1", "a'b", None])
        self.assertEqual(parse_array("[(1,'a'), ( 2 , ')' )]"), ["(1,'a')", "( 2 , ')' )"])
        self.assertEqual(parse_array("([1,[2]],{'k':(3)})"), ["[1,[2]]", "{'k':(3)}"])
        self.assertEqual(parse_array("(1,)"), ["1"])
        self.assertEqual(parse_array("()"), [])
        for s in ("(1,2,)", "[1,]", "(1]", "[(1,2]", "[(1,'a)]", "{1}", "[1,,2]", "[(1)2]"):
            with self.assertRaises(ValueError):
                parse_array(s)
