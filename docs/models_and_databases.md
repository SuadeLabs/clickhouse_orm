Models and Databases
====================

Models represent ClickHouse tables, allowing you to work with them using familiar pythonic syntax.

Database instances connect to a specific ClickHouse database for running queries, inserting data and other operations.

Defining Models
---------------

Models are defined in a way reminiscent of Django's ORM, by subclassing `Model`:
```python
from clickhouse_orm import Model, StringField, DateField, Float32Field, MergeTree

class Person(Model):

    first_name = StringField()
    last_name = StringField()
    birthday = DateField()
    height = Float32Field()

    engine = MergeTree('birthday', ('first_name', 'last_name', 'birthday'))
```

The columns in the database table are represented by model fields. Each field has a type, which matches the type of the corresponding database column. All the supported fields types are listed [here](field_types.md).

A model must have an `engine`, which determines how its table is stored on disk (if at all), and what capabilities it has. For more details about table engines see [here](table_engines.md).

### Default values

Each field has a "natural" default value - empty string for string fields, zero for numeric fields etc. To specify a different value use the `default` parameter:

        first_name = StringField(default="anonymous")

For additional details see [here](field_options.md).

### Null values

To allow null values in a field, wrap it inside a `NullableField`:

        birthday = NullableField(DateField())

In this case, the default value for that field becomes `null` unless otherwise specified.

For more information about `NullableField` see [Field Types](field_types.md).

### Materialized fields

The value of a materialized field is calculated from other fields in the model. For example:

        year_born = Int16Field(materialized=F.toYear(birthday))

Materialized fields are read-only, meaning that their values are not sent to the database when inserting records.

For additional details see [here](field_options.md).

### Alias fields

An alias field is a field whose value is calculated by ClickHouse on the fly, as a function of other fields. It is not physically stored by the database. For example:

        weekday_born = field.UInt8Field(alias=F.toDayOfWeek(birthday))

Alias fields are read-only, meaning that their values are not sent to the database when inserting records.

For additional details see [here](field_options.md).

### Table Names

The table name used for the model is its class name, converted to lowercase. To override the default name, implement the `table_name` method:
```python
class Person(Model):

    ...

    @classmethod
    def table_name(cls):
        return 'people'
```

### Model Constraints

It is possible to define constraints which ClickHouse verifies when data is inserted. Trying to insert invalid records will raise a `ServerError`. Each constraint has a name and an expression to validate. For example:
```python
class Person(Model):

    ...

    # Ensure that the birthday is not a future date
    birthday_is_in_the_past = Constraint(birthday <= F.today())
```

### Data Skipping Indexes

Models that use an engine from the `MergeTree` family can define additional indexes over one or more columns or expressions. These indexes are used in SELECT queries for reducing the amount of data to read from the disk by skipping big blocks of data that do not satisfy the query's conditions.

For example:
```python
class Person(Model):

    ...

    # A minmax index that can help find people taller or shorter than some height
    height_index = Index(height, type=Index.minmax(), granularity=2)

    # A trigram index that can help find substrings inside people names
    names_index = Index((F.lower(first_name), F.lower(last_name)),
                        type=Index.ngrambf_v1(3, 256, 2, 0), granularity=1)
```


Using Models
------------

Once you have a model, you can create model instances:

    >>> dan = Person(first_name='Dan', last_name='Schwartz')
    >>> suzy = Person(first_name='Suzy', last_name='Jones')
    >>> dan.first_name
    u'Dan'

When values are assigned to model fields, they are immediately converted to their Pythonic data type. In case the value is invalid, a `ValueError` is raised:

    >>> suzy.birthday = '1980-01-17'
    >>> suzy.birthday
    datetime.date(1980, 1, 17)
    >>> suzy.birthday = 0.5
    ValueError: Invalid value for DateField - 0.5
    >>> suzy.birthday = '1922-05-31'
    ValueError: DateField out of range - 1922-05-31 is not between 1970-01-01 and 2105-12-31

Inserting to the Database
-------------------------

To write your instances to ClickHouse, you need a `Database` instance:

    from clickhouse_orm import Database

    db = Database('my_test_db')

This automatically connects to <http://localhost:8123> and creates a database called my_test_db, unless it already exists. If necessary, you can specify a different database URL and optional credentials:

    db = Database('my_test_db', db_url='http://192.168.1.1:8050', username='scott', password='tiger')

Using the `Database` instance you can create a table for your model, and insert instances to it:

    db.create_table(Person)
    db.insert([dan, suzy])

The `insert` method can take any iterable of model instances, but they all must belong to the same model class.

The DDL for a model can also be generated without a database connection, e.g. to review it or to apply it with another tool. By default, the SQL targets a modern ClickHouse server; pass a `ServerCapabilities` instance to disable optional features for older servers:

    from clickhouse_orm import ServerCapabilities

    print(Person.create_table_sql('my_test_db'))
    print(Person.create_table_sql('my_test_db', ServerCapabilities(has_codec_support=False)))
    print(Person.drop_table_sql('my_test_db'))

A connected `Database` exposes the capabilities of its server as `db.capabilities`.

Creating a read-only database is also supported. Such a `Database` instance can only read data, and cannot modify data or schemas:

    db = Database('my_test_db', readonly=True)

Reading from the Database
-------------------------

Loading model instances from the database is simple:

    for person in db.select("SELECT * FROM my_test_db.person", model_class=Person):
        print(person.first_name, person.last_name)

Do not include a `FORMAT` clause in the query, since the ORM automatically sets the format to `TabSeparatedWithNamesAndTypes`.

It is possible to select only a subset of the columns, and the rest will receive their default values:

    for person in db.select("SELECT first_name FROM my_test_db.person WHERE last_name='Smith'", model_class=Person):
        print(person.first_name)

The ORM provides a way to build simple queries without writing SQL by hand. The previous snippet can be written like this:

    for person in Person.objects_in(db).filter(Person.last_name == 'Smith').only('first_name'):
        print(person.first_name)

See [Querysets](querysets.md) for more information.


Reading without a Model
-----------------------

When running a query, specifying a model class is not required. In case you do not provide a model class, an ad-hoc class will be defined based on the column names and types returned by the query:

    for row in db.select("SELECT max(height) as max_height FROM my_test_db.person"):
        print(row.max_height)

This is a very convenient feature that saves you the need to define a model for each query, while still letting you work with Pythonic column values and an elegant syntax.

It is also possible to generate a model class on the fly for an existing table in the database using `get_model_for_table`. This is particularly useful for querying system tables, for example:

    QueryLog = db.get_model_for_table('query_log', system_table=True)
    for row in QueryLog.objects_in(db).filter(QueryLog.query_duration_ms > 10000):
        print(row.query)

Reading Rows
------------

`select_rows` skips model construction entirely. It returns a `RowResult` which exposes the column metadata up
front and yields one plain tuple per row, similar to `clickhouse_driver`'s `execute(..., with_column_types=True)`:

    result = db.select_rows("SELECT first_name, count() FROM $db.person GROUP BY first_name")
    print(result.columns)       # [('first_name', 'String'), ('count()', 'UInt64')]
    print(result.column_names)  # ['first_name', 'count()']
    for first_name, total in result:
        print(first_name, total)

Unlike `select`, the query is sent immediately, so server errors are raised by `select_rows` itself. Rows are
streamed from the response and can only be iterated once. Duplicate column names (e.g. `SELECT 1, 1`) are preserved.

Values use the same Python types as `clickhouse_driver`:

- `DateTime` / `DateTime64` without an explicit timezone are naive datetimes in the server's timezone; columns with
  an explicit timezone (e.g. `DateTime('UTC')`) are timezone-aware
- `Enum` values are returned as their name (`str`), `Bool` as `bool`, `Nullable` NULLs as `None`
- `FixedString` values have trailing null bytes removed; strings which are not valid UTF-8 are returned as `bytes`
- Types which are not parsed yet (`Tuple`, `Map`, nested arrays, `JSON`, ...) are returned as their text representation
  (the [native driver](#the-native-driver) returns them as Python objects)

SQL Placeholders
----------------

There are a couple of special placeholders that you can use inside the SQL to make it easier to write: `$db` and `$table`. The first one is replaced by the database name, and the second is replaced by the table name (but is available only when the model is specified).

So instead of this:

    db.select("SELECT * FROM my_test_db.person", model_class=Person)

you can use:

    db.select("SELECT * FROM $db.$table", model_class=Person)

Note: normally it is not necessary to specify the database name, since it's already sent in the query parameters to ClickHouse. It is enough to specify the table name.

Query Parameters
----------------

Rather than formatting values into the SQL, you can use ClickHouse's `{name:Type}` placeholders and pass the values
separately with `params`. The values are sent to the server alongside the query and are never parsed as SQL, which
makes this the safe way to use values from untrusted sources. `select`, `select_rows`, `raw`, `count` and
`paginate` all accept `params`:

    db.select("SELECT * FROM $table WHERE first_name = {name:String} AND height > {height:Float32}",
              model_class=Person, params={"name": name, "height": 1.8})
    db.count(Person, conditions="birthday >= {since:Date}", params={"since": date(2000, 1, 1)})

Supported values are `None` (for `Nullable` types), strings, booleans, numbers, `Decimal`, dates, datetimes (naive
ones are treated as UTC, as elsewhere in the ORM), UUIDs, IP addresses, enums (sent by name), lists (arrays),
tuples and dicts (maps). A datetime with microseconds can only be passed to a `DateTime64` placeholder. For other
types, pass an `EncodedParam` holding text in ClickHouse's escaped format, e.g. `EncodedParam.for_field(field, value)`
to encode a value exactly like a model field does.

Querysets can bind their values as parameters too; see [Parameterized Querysets](querysets.md#parameterized-querysets).

Counting
--------

The `Database` class also supports counting records easily:

    >>> db.count(Person)
    117
    >>> db.count(Person, conditions="height > 1.90")
    6

Pagination
----------

It is possible to paginate through model instances:

    >>> order_by = 'first_name, last_name'
    >>> page = db.paginate(Person, order_by, page_num=1, page_size=10)
    >>> print(page.number_of_objects)
    2507
    >>> print(page.pages_total)
    251
    >>> for person in page.objects:
    >>>     # do something

The `paginate` method returns a `namedtuple` containing the following fields:

-   `objects` - the list of objects in this page
-   `number_of_objects` - total number of objects in all pages
-   `pages_total` - total number of pages
-   `number` - the page number, starting from 1; the special value -1 may be used to retrieve the last page
-   `page_size` - the number of objects per page

You can optionally pass conditions to the query:

    >>> page = db.paginate(Person, order_by, page_num=1, page_size=100, conditions='height > 1.90')

Note that `order_by` must be chosen so that the ordering is unique, otherwise there might be inconsistencies in the pagination (such as an instance that appears on two different pages).


Drivers
-------

A `Database` talks to ClickHouse through a driver. By default it creates a `RequestsDriver`, which uses the HTTP
interface (with the `db_url`, `username`, `password`, `timeout` and `verify_ssl_cert` arguments) and exchanges data
in the `TabSeparated` format. To use another driver, pass it as `driver` instead of the connection arguments:

    db = Database('my_test_db', driver=my_driver)

### The native driver

`NativeDriver` uses ClickHouse's native TCP protocol, via the [clickhouse-driver](https://clickhouse-driver.readthedocs.io)
library. It is installed with the `native` extra, and must be imported from `clickhouse_orm.native`:

    pip install clickhouse_orm[native]

    from clickhouse_orm.native import NativeDriver

    db = Database('my_test_db', driver=NativeDriver('localhost', port=9000, user='me', password='secret'))
    db = Database('my_test_db', driver=NativeDriver.from_url('clickhouse://me:secret@localhost:9000'))

Other keyword arguments (such as `secure`, `compression` or `settings`) are passed to `clickhouse_driver.Client`.
Values are sent and received in ClickHouse's binary format rather than as text, and the rest of the ORM works as
with the default driver. The differences are:

- Results are read in full before they are returned, so large results are held in memory. Like
  `clickhouse_driver.Client`, a driver must not be used by several threads at once.
- `raw` returns an approximation of the `TabSeparated` output, and any `FORMAT` clause in the query is ignored.
- `select_rows` returns values of every type as Python objects (e.g. tuples, dicts and nested lists), where the
  default driver returns the text of types it does not parse.
- Query parameters require a server version which supports them over the native protocol; older servers raise
  a `DatabaseException`.

### Performance

The table below compares the drivers for 100,000 rows of a model with 101 columns: 15 `Int32`, 10 `UInt64`,
15 `Float64`, 5 `Float32`, 20 `String`, 5 `LowCardinality(String)`, 10 `Date`, 10 `DateTime`, 5 `Enum8`,
3 `Decimal(18, 4)` and 2 `Nullable(Int32)` columns, plus a `UInt32` id (about 76 MB uncompressed in ClickHouse).

| Operation | Driver | Wall time (s) | CPU time (s) | Rows/s | Peak memory (MB) |
|---|---|---:|---:|---:|---:|
| Build 100k model instances | - | 21.78 | 21.77 | 4,592 | 731 |
| `insert` a list of instances | HTTP | 7.19 | 6.88 | 13,913 | 3 |
| `insert` a list of instances | native | 5.41 | 4.82 | 18,493 | 3 |
| `insert` from a generator of new instances | HTTP | 27.54 | 27.20 | 3,631 | 4 |
| `insert` from a generator of new instances | native | 25.77 | 25.17 | 3,880 | 11 |
| Iterate over `select` (model instances) | HTTP | 24.02 | 23.91 | 4,163 | 0 |
| Iterate over `select` (model instances) | native | 17.04 | 16.96 | 5,870 | 554 |
| Iterate over `select_rows` (tuples) | HTTP | 10.54 | 10.40 | 9,490 | 0 |
| Iterate over `select_rows` (tuples) | native | 5.29 | 5.23 | 18,911 | 554 |

Each figure is the median of 3 runs, each in a fresh process. CPU time is the client's (the server's share is
roughly the difference to the wall time), and peak memory is the growth of the process's peak resident memory
during the operation, so it excludes model instances which already existed. The server and client ran on the same
machine (Python 3.11.15, ClickHouse 25.8.33.6, clickhouse-driver 0.2.10, AMD Ryzen 7 PRO 4750U), with the default
`batch_size` of 1000. To reproduce, run
`python scripts/benchmark.py --http-url http://localhost:8123/ --native-url clickhouse://localhost:9000`.

Some conclusions:

- Nearly all the time is spent in Python rather than on the server or network, and most of it building and
  validating model instances: creating 100k instances takes about 22 s whichever driver is used, which is why
  `insert` from a generator and `select` are much slower than `insert` from an existing list and `select_rows`.
  When model instances are not needed, `select_rows` is 2.3x (HTTP) to 3.2x (native) faster than `select`.
- The native driver is 1.3x to 2x faster than HTTP, since values do not need to be formatted as text and parsed.
- Both drivers stream inserts in chunks of `batch_size` rows, so inserting from an iterator uses little memory
  (holding 100k instances in a list takes about 730 MB).
- The HTTP driver streams `select` and `select_rows` results, so iterating over them uses constant memory. The
  native driver reads the whole result first (about 5.5 KB per row here). For large results, use the HTTP driver
  or split the query into smaller ones (e.g. by ranges of the table's sorting key).

### Custom drivers

Other drivers can be written by subclassing `clickhouse_orm.driver.Driver` and implementing `send`. It receives
the SQL statement, the insert data (if any), the query settings, and the values of the query parameters in
ClickHouse's escaped text format. `Database` passes the name of its database in the settings as `database`, and
expects server errors to be raised as `ServerError`.

A driver's `codec` class attribute determines the data it exchanges. The default, `TSVCodec`, sends insert data
as an iterable of `TabSeparated` byte chunks, and needs responses with a `text` property and an `iter_lines()`
method (see `DriverResponse`). For example, a driver using the HTTP interface via the standard library:

    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from urllib.request import urlopen

    from clickhouse_orm.driver import Driver
    from clickhouse_orm.exceptions import ServerError

    class UrllibResponse:
        def __init__(self, text):
            self.text = text

        def iter_lines(self):
            return iter(self.text.encode().splitlines())

    class UrllibDriver(Driver):
        def __init__(self, url):
            self.url = url

        def send(self, query, data=None, settings=None, stream=False, params=None):
            url_params = dict(settings or {})
            url_params.update(('param_' + name, value) for name, value in (params or {}).items())
            if data is None:
                body = query.encode()
            else:
                url_params['query'] = query
                body = b''.join(data)
            try:
                with urlopen(self.url + '?' + urlencode(url_params), data=body) as response:
                    return UrllibResponse(response.read().decode())
            except HTTPError as e:
                raise ServerError(e.read().decode())

    db = Database('my_test_db', driver=UrllibDriver('http://localhost:8123/'))

---

[<< Overview](index.md) | [Table of Contents](toc.md) | [Expressions >>](expressions.md)
