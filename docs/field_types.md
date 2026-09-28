Field Types
===========

See: [ClickHouse Documentation](https://clickhouse.tech/docs/en/sql-reference/data-types/)

The following field types are supported:

| Class              | DB Type    | Pythonic Type         | Comments
| ------------------ | ---------- | --------------------- | -----------------------------------------------------
| StringField        | String     | str                   | Encoded as UTF-8 when written to ClickHouse
| FixedStringField   | FixedString| str                   | Encoded as UTF-8 when written to ClickHouse
| DateField          | Date       | datetime.date         | Range 1970-01-01 to 2105-12-31
| DateTimeField      | DateTime   | datetime.datetime     | Minimal value is 1970-01-01 00:00:00; Timezone aware
| DateTime64Field    | DateTime64 | datetime.datetime     | Minimal value is 1970-01-01 00:00:00; Timezone aware
| Int8Field          | Int8       | int                   | Range -128 to 127
| Int16Field         | Int16      | int                   | Range -32768 to 32767
| Int32Field         | Int32      | int                   | Range -2147483648 to 2147483647
| Int64Field         | Int64      | int                   | Range -9223372036854775808 to 9223372036854775807
| UInt8Field         | UInt8      | int                   | Range 0 to 255
| UInt16Field        | UInt16     | int                   | Range 0 to 65535
| UInt32Field        | UInt32     | int                   | Range 0 to 4294967295
| UInt64Field        | UInt64     | int                   | Range 0 to 18446744073709551615
| Float32Field       | Float32    | float                 |
| Float64Field       | Float64    | float                 |
| DecimalField       | Decimal    | Decimal               | Pythonic values are rounded to fit the scale of the database field
| Decimal32Field     | Decimal32  | Decimal               | Ditto
| Decimal64Field     | Decimal64  | Decimal               | Ditto
| Decimal128Field    | Decimal128 | Decimal               | Ditto
| UUIDField          | UUID       | uuid.UUID             |
| IPv4Field          | IPv4       | ipaddress.IPv4Address |
| IPv6Field          | IPv6       | ipaddress.IPv6Address |
| Enum8Field         | Enum8      | Enum                  | See below
| Enum16Field        | Enum16     | Enum                  | See below
| ArrayField         | Array      | list                  | See below
| TupleField         | Tuple      | tuple                 | See below
| NullableField      | Nullable   | See below             | See below


DateTimeField and Time Zones
----------------------------

`DateTimeField` and `DateTime64Field` can accept a `timezone` parameter (either the timezone name or a `pytz` timezone instance). This timezone will be used as the column timezone in ClickHouse. If not provided, the fields will use the timezone defined in the database configuration.

A `DateTimeField` and `DateTime64Field` can be assigned values from one of the following types:

-   datetime
-   date
-   integer - number of seconds since the Unix epoch
-   float (DateTime64Field only) - number of seconds and microseconds since the Unix epoch
-   string in `YYYY-MM-DD HH:MM:SS` format or [ISO 8601](https://en.wikipedia.org/wiki/ISO_8601)-compatible format

Values are either *naive* (a wall-clock time without a timezone) or *aware* (an absolute point in time), and the ORM treats them the same way that ClickHouse does:

-   A naive value is a wall-clock time **in the column's timezone** - the field's `timezone` if it has one, and otherwise the server's timezone. Naive values are written to the database as text (e.g. `'2020-06-11 04:00:00'`) and ClickHouse interprets them in the column's timezone.
-   An aware value is an absolute point in time, written to the database as a Unix timestamp. Integers, floats and strings with a UTC offset (e.g. `2020-06-11T04:00:00+03:00`) are aware too.

When a value is assigned:

-   Fields **without** a timezone keep naive values naive, and aware values unchanged.
-   Fields **with** a timezone localize naive values to that timezone, and keep aware values unchanged.

When reading from the database, the column's type decides the result, whichever model is used for the query:

-   Columns without a timezone (e.g. `DateTime`) are read as naive wall-clock times in the server's timezone - exactly what was inserted, if a naive value was inserted.
-   Columns with a timezone (e.g. `DateTime('Europe/Madrid')`) are read as aware values in that timezone.

```python
class Event(Model):
    local_time = DateTimeField()                          # DateTime
    madrid_time = DateTimeField(timezone='Europe/Madrid') # DateTime('Europe/Madrid')

event = Event(local_time='2020-06-11 04:00:00', madrid_time='2020-06-11 04:00:00')
event.local_time   # datetime(2020, 6, 11, 4, 0)
event.madrid_time  # datetime(2020, 6, 11, 4, 0, tzinfo=<DstTzInfo 'Europe/Madrid' CEST+2:00:00 DST>)
db.insert([event])
event = Event.objects_in(db)[0]  # both values are read back unchanged
```

Some things to be aware of:

-   Aware values inserted into columns without a timezone are read back as naive values in the server's timezone. This includes a field's default value (the Unix epoch), so `Event().local_time` is aware but reads back as `datetime(1970, 1, 1, 0, 0)` on a UTC server.
-   When a model field has a timezone but the column in the query doesn't (or has a different one), the result follows the column. It is best to keep model definitions in sync with the table schema.
-   Naive and aware values cannot be compared in Python (`==` is always false and `<` raises `TypeError`), so be consistent in how you use each field.
-   Some older ClickHouse versions add the server's timezone to the type of function results (e.g. `toStartOfHour(dt)` returns `DateTime('UTC')`), so these results are aware.

The same rules apply to [query parameters](models_and_databases.md#query-parameters) and to both [drivers](models_and_databases.md#drivers).

It is strongly recommended to set the server timezone to UTC, in order to prevent confusion and subtle bugs. Conversion to a different timezone should only be performed when the value needs to be displayed.


Working with enum fields
------------------------

`Enum8Field` and `Enum16Field` provide support for working with ClickHouse enum columns. They accept strings or integers as values, and convert them to the matching Pythonic Enum member.

Example of a model with an enum field:

```python
Gender = Enum('Gender', 'male female unspecified')

class Person(Model):

    first_name = StringField()
    last_name = StringField()
    birthday = DateField()
    gender = Enum32Field(Gender)

    engine = MergeTree('birthday', ('first_name', 'last_name', 'birthday'))

suzy = Person(first_name='Suzy', last_name='Jones', gender=Gender.female)
```

Working with array fields
-------------------------

You can create array fields containing any data type, for example:

```python
class SensorData(Model):

    date = DateField()
    temperatures = ArrayField(Float32Field())
    humidity_levels = ArrayField(UInt8Field())

    engine = MergeTree('date', ('date',))

data = SensorData(date=date.today(), temperatures=[25.5, 31.2, 28.7], humidity_levels=[41, 39, 66])
```

Note that multidimensional arrays are not supported yet by the ORM.

Working with tuple fields
-------------------------

A `TupleField` holds a fixed number of elements, each with its own field. Pass a list of fields, or a list of `(name, field)` pairs for a named tuple:

```python
class Measurement(Model):

    date = DateField()
    location = TupleField([Float64Field(), Float64Field()])                        # Tuple(Float64, Float64)
    reading = TupleField([('sensor', StringField()), ('value', NullableField(Float32Field()))])
    history = ArrayField(TupleField([DateTimeField(), Float32Field()]))

    engine = MergeTree('date', ('date',))

m = Measurement(date=date.today(), location=(51.5, -0.12), reading={'sensor': 'a1', 'value': None})
m.reading   # ('a1', None)
```

Values are Python tuples, as in `clickhouse_driver`, and each element is converted by its field (so enum elements are enum members, datetimes follow the [time zone rules](#datetimefield-and-time-zones), and so on). A list can be assigned too, and named tuples also accept a dict of the elements by name. The default value is a tuple of the elements' defaults.

Tuple fields can contain arrays and other tuples, and can be used as the elements of arrays. They can be compared in querysets (e.g. `filter(location=(51.5, -0.12))`), including as [query parameters](models_and_databases.md#query-parameters).

Note that older ClickHouse versions (e.g. 21.3) store an array of *named* tuples as `Nested` columns (one array column per element, such as `history.when` and `history.value`) unless the `flatten_nested` setting is disabled, so use unnamed tuples in arrays when targeting them.

Working with nullable fields
----------------------------
[ClickHouse provides a NULL value support](https://clickhouse.tech/docs/en/sql-reference/data-types/nullable/).

Wrapping another field in a `NullableField` makes it possible to assign `None` to that field. For example:

```python
class EventData(Model):

    date = DateField()
    comment = NullableField(StringField(), extra_null_values={''})
    score = NullableField(UInt8Field())
    serie = NullableField(ArrayField(UInt8Field()))

    engine = MergeTree('date', ('date',))


score_event = EventData(date=date.today(), comment=None, score=5, serie=None)
comment_event = EventData(date=date.today(), comment='Excellent!', score=None, serie=None)
another_event = EventData(date=date.today(), comment='', score=None, serie=None)
action_event = EventData(date=date.today(), comment='', score=None, serie=[1, 2, 3])
```

The `extra_null_values` parameter is an iterable of additional values that should be converted
to `None`.

NOTE: `ArrayField` of `NullableField` is not supported. Also `EnumField` cannot be nullable.

NOTE: Using `Nullable` almost always negatively affects performance, keep this in mind when designing your databases.

Working with LowCardinality fields
----------------------------------
Starting with version 19.0 ClickHouse offers a new type of field to improve the performance of queries
and compaction of columns for low entropy data.

[More specifically](https://github.com/tech/ClickHouse/issues/4074) LowCardinality data type builds dictionaries automatically. It can use multiple different dictionaries if necessarily.
If the number of distinct values is pretty large, the dictionaries become local, several different dictionaries will be used for different ranges of data. For example, if you have too many distinct values in total, but only less than about a million values each day - then the queries by day will be processed efficiently, and queries for larger ranges will be processed rather efficiently.

LowCardinality works independently of (generic) fields compression.
LowCardinality fields are subsequently compressed as usual.
The compression ratios of LowCardinality fields for text data may be significantly better than without LowCardinality.

LowCardinality will give performance boost, in the form of processing speed, if the number of distinct values is less than a few millions. This is because data is processed in dictionary encoded form.

You can find further information [here](https://clickhouse.tech/docs/en/sql-reference/data-types/lowcardinality/).

Usage example:
```python
class LowCardinalityModel(Model):
    date       = DateField()
    string     = LowCardinalityField(StringField())
    nullable   = LowCardinalityField(NullableField(StringField()))
    array      = ArrayField(LowCardinalityField(DateField()))
    ...
```

Note: `LowCardinality` field with an inner array field is not supported. Use an `ArrayField` with a `LowCardinality` inner field as seen in the example.

Creating custom field types
---------------------------
Sometimes it is convenient to use data types that are supported in Python, but have no corresponding column type in ClickHouse. In these cases it is possible to define a custom field class that knows how to convert the Pythonic object to a suitable representation in the database, and vice versa.

For example, we can create a BooleanField which will hold `True` and `False` values, but write them to the database as 0 and 1 (in a `UInt8` column). For this purpose we'll subclass the `Field` class, and implement two methods:

- `to_python` which converts any supported value to a `bool`. The method should know how to handle strings (which typically come from the database), booleans, and possibly other valid options. In case the value is not supported, it should raise a `ValueError`.
- `to_db_string` which converts a `bool` into a string for writing to the database.

Here's the full implementation:

```python
from clickhouse_orm import Field

class BooleanField(Field):

    # The ClickHouse column type to use
    db_type = 'UInt8'

    # The default value
    class_default = False

    def to_python(self, value, timezone_in_use):
        # Convert valid values to bool
        if value in (1, '1', True):
            return True
        elif value in (0, '0', False):
            return False
        else:
            raise ValueError('Invalid value for BooleanField: %r' % value)

    def to_db_string(self, value, quote=True):
        # The value was already converted by to_python, so it's a bool
        return '1' if value else '0'
```

---

[<< Field Options](field_options.md) | [Table of Contents](toc.md) | [Table Engines >>](table_engines.md)
