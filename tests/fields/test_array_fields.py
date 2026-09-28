from __future__ import annotations

import unittest
from datetime import date

from clickhouse_orm.database import Database
from clickhouse_orm.engines import MergeTree
from clickhouse_orm.fields import ArrayField, DateField, Int32Field, NullableField, StringField
from clickhouse_orm.models import Model


class ArrayFieldsTest(unittest.TestCase):
    def setUp(self):
        self.database = Database("test-db", log_statements=True)
        self.database.create_table(ModelWithArrays)

    def tearDown(self):
        self.database.drop_database()

    def test_insert_and_select(self):
        instance = ModelWithArrays(
            date_field="2016-08-30",
            arr_str=["goodbye,", "cruel", "world", "special chars: ,\"\\'` \n\t\\[]"],
            arr_date=["2010-01-01"],
        )
        self.database.insert([instance])
        query = "SELECT * from $db.modelwitharrays ORDER BY date_field"
        for model_cls in (ModelWithArrays, None):
            results = list(self.database.select(query, model_cls))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].arr_str, instance.arr_str)
            self.assertEqual(results[0].arr_int, instance.arr_int)
            self.assertEqual(results[0].arr_date, instance.arr_date)

    def test_strings_with_special_characters(self):
        instance = ModelWithArrays(date_field="2016-08-30", arr_str=TRICKY_STRINGS)
        self.database.insert([instance])
        query = "SELECT * from $db.modelwitharrays"
        for model_cls in (ModelWithArrays, None):
            (result,) = self.database.select(query, model_cls)
            self.assertEqual(result.arr_str, TRICKY_STRINGS)
        (row,) = self.database.select_rows("SELECT arr_str FROM $db.modelwitharrays")
        self.assertEqual(row, (TRICKY_STRINGS,))
        # The values can be compared in queries too
        for parameterized in (False, True):
            qs = ModelWithArrays.objects_in(self.database).filter(arr_str=TRICKY_STRINGS)
            self.assertEqual(qs.parameterized(parameterized).count(), 1)

    def test_nulls(self):
        self.database.create_table(ModelWithNullableArrays)
        instances = [
            ModelWithNullableArrays(id=1, arr_int=[1, None, 3], arr_str=["NULL", None, "a'b", "\\N"]),
            ModelWithNullableArrays(id=2, arr_int=[None], arr_str=[]),
        ]
        self.database.insert(instances)
        query = "SELECT * from $db.modelwithnullablearrays ORDER BY id"
        for model_cls in (ModelWithNullableArrays, None):
            results = list(self.database.select(query, model_cls))
            self.assertEqual([r.arr_int for r in results], [i.arr_int for i in instances])
            self.assertEqual([r.arr_str for r in results], [i.arr_str for i in instances])
        rows = list(self.database.select_rows("SELECT arr_int, arr_str FROM $db.modelwithnullablearrays ORDER BY id"))
        self.assertEqual(rows, [(i.arr_int, i.arr_str) for i in instances])
        for parameterized in (False, True):
            qs = ModelWithNullableArrays.objects_in(self.database).filter(arr_int=[1, None, 3])
            self.assertEqual([r.id for r in qs.parameterized(parameterized)], [1])

    def test_conversion(self):
        instance = ModelWithArrays(arr_int=("1", "2", "3"), arr_date=["2010-01-01"])
        self.assertEqual(instance.arr_str, [])
        self.assertEqual(instance.arr_int, [1, 2, 3])
        self.assertEqual(instance.arr_date, [date(2010, 1, 1)])

    def test_assignment_error(self):
        instance = ModelWithArrays()
        for value in (7, "x", [date.today()], ["aaa"], [None]):
            with self.assertRaises(ValueError):
                instance.arr_int = value

    def test_parse_array(self):
        from clickhouse_orm.utils import parse_array

        self.assertEqual(parse_array("[]"), [])
        self.assertEqual(parse_array("[1, 2, 395, -44]"), ["1", "2", "395", "-44"])
        self.assertEqual(parse_array("['big','mouse','','!']"), ["big", "mouse", "", "!"])
        self.assertEqual(parse_array("['\\r\\n\\0\\t\\b']"), ["\r\n\0\t\b"])
        self.assertEqual(parse_array("['a\\'b','\\\\','x\\\\\\'','[,]']"), ["a'b", "\\", "x\\'", "[,]"])
        self.assertEqual(parse_array("[1,NULL]"), ["1", None])
        self.assertEqual(parse_array("['NULL',NULL]"), ["NULL", None])
        self.assertEqual(parse_array("['\\xc3\\xa9', 'é']"), ["é", "é"])
        for s in ("", "[", "]", "[1, 2", "3, 4]", "['aaa', 'aaa]", "['a\\']"):
            with self.assertRaises(ValueError):
                parse_array(s)

    def test_to_db_string(self):
        field = ArrayField(NullableField(StringField()))
        self.assertEqual(field.to_db_string(["a'b", "\\", None]), "['a\\'b', '\\\\', NULL]")
        instance = ModelWithNullableArrays(arr_int=[1, None], arr_str=["a\tb"])
        self.assertEqual(instance.to_tsv(), "0\t[1, NULL]\t['a\\tb']")

    def test_invalid_inner_field(self):
        for x in (DateField, None, "", ArrayField(Int32Field())):
            with self.assertRaises(AssertionError):
                ArrayField(x)


class ModelWithArrays(Model):
    date_field = DateField()
    arr_str = ArrayField(StringField())
    arr_int = ArrayField(Int32Field())
    arr_date = ArrayField(DateField())

    engine = MergeTree("date_field", ("date_field",))


class ModelWithNullableArrays(Model):
    id = Int32Field()
    arr_int = ArrayField(NullableField(Int32Field()))
    arr_str = ArrayField(NullableField(StringField()))

    engine = MergeTree(order_by=("id",), partition_key=("tuple()",))


# Strings which need escaping inside arrays, or which could be confused with the syntax of arrays or TSV
TRICKY_STRINGS = [
    "a'b",
    "'",
    "back\\slash",
    "\\",
    "ends with backslash\\",
    "\\'",
    "\\N",
    "NULL",
    "",
    "tab\there\nnewline\r\0",
    "[', ']",
    "unicode: é, 日本",
]
