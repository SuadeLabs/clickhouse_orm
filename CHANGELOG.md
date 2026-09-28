Change Log
==========

Unreleased
----------
- All HTTP I/O now goes through a pluggable `Driver` (`clickhouse_orm.driver`); `Database.driver` defaults to `RequestsDriver`
- `DatabaseException` and `ServerError` moved to `clickhouse_orm.exceptions` (still importable from `clickhouse_orm.database`)
- `Database.request_session` is now a read-only property proxying `Database.driver.session`
- INSERT statements are now logged when `log_statements=True`
- bugfix: `Database.server_version` no longer drops the final version component
- DDL generation no longer needs a live `Database`: `Model.create_table_sql(db_name, capabilities=None)`,
  `Model.drop_table_sql(db_name)`, `Engine.create_table_sql(db_name, capabilities=None)` and
  `Field.get_sql(with_default_expression=True, *, capabilities=None)`
- New `ServerCapabilities` (`clickhouse_orm.compiler`) describing optional server features; `Database.capabilities`
  is derived from the server version. `Database.has_codec_support` / `has_low_cardinality_support` are now read-only
  properties proxying it
- `$db` / `$table` substitution moved to `clickhouse_orm.compiler.substitute`; migrations use qualified table names
- bugfix: pre-1.1.54310 `MergeTree` syntax raised a `TypeError`
- Serialisation moved into a pluggable `Codec` (`clickhouse_orm.codec`); `Database.codec` defaults to `TSVCodec`,
  which owns the wire formats, TSV header parsing and insert batching
- `insert(batch_size=n)` now sends exactly `n` rows per chunk (previously the first chunk held one row fewer)
- `QuerySet` no longer depends on `Database`: it runs its SQL through any `Executor` (`clickhouse_orm.executor`), a
  protocol with `select`, `raw` and `count` which `Database` implements. `Model.objects_in` accepts any executor,
  so query building can be unit-tested without a server. `QuerySet._database` remains as an alias of `_executor`
- Query parameters: `Database.select`, `select_rows`, `raw`, `count` and `paginate` accept `params` for ClickHouse's
  `{name:Type}` placeholders; values are encoded by `clickhouse_orm.params.format_param` (or passed pre-encoded as
  `EncodedParam`) and sent by the driver (`Driver.send(..., params=...)`, as `param_<name>` URL parameters over HTTP)
- `QuerySet.parameterized()` binds filter values and string / date / datetime function arguments as query parameters
  instead of inlining them; `QuerySet.as_sql_with_params()` returns the `(sql, params)` pair. Executors receive
  `params` only from parameterized querysets
- New `Database.select_rows(query, settings=None)` returning a `RowResult` of plain tuples plus `(name, type)`
  column metadata, with values typed like `clickhouse_driver` (see "Reading Rows" in the docs)
- Pluggable drivers: `Database(db_name, driver=...)` uses any `Driver` subclass instead of the default
  `RequestsDriver` (see "Drivers" in the docs). A driver's `codec` attribute selects its `Codec`
- New optional `NativeDriver` (`clickhouse_orm.native`) using the native TCP protocol via `clickhouse-driver`;
  install with `pip install clickhouse_orm[native]`
- `scripts/benchmark.py` compares the drivers for bulk inserts and selects (results under "Performance" in the docs)
- The test suite can be run against the native driver with `pytest --driver=native`
- bugfix: deep-copying a `QuerySet` (e.g. when used as a subquery filter) no longer copies its database
- bugfix: `select_rows` array elements are now typed like `clickhouse_driver` (e.g. enum names, naive datetimes)
- bugfix: arrays of `Nullable` fields containing `None` could not be inserted or read (NULLs inside arrays are now
  written as the `NULL` keyword, in TSV and SQL alike)
- bugfix: array elements containing quotes or backslashes were corrupted when read, since the TSV codec unescaped
  array cells which ClickHouse does not escape for TSV. `parse_array` now unescapes quoted elements, and returns
  `None` for `NULL`. `select_rows` returns `Tuple` / `Map` text exactly as ClickHouse writes it

**Deprecations / backwards incompatible changes**

- Passing a `Database` to `create_table_sql`, `drop_table_sql`, `Engine.create_table_sql` or `Field.get_sql(db=...)`
  still works but emits a `DeprecationWarning`
- Custom subclasses should override the new internal hooks instead of the public methods:
  `Model._create_table_sql(db_name, capabilities)`, `Engine._create_table_sql(db_name, capabilities)` and
  `Field._get_sql(with_default_expression, capabilities)`. Overrides of the old public methods that expect a
  `Database` argument will no longer be called with one
- `Codec` API: `encode_inserts(model_class, instances, batch_size)` replaces `encode` / `insert_format` (which remain
  on `TSVCodec`), `decode` / `decode_rows` receive the driver response instead of lines, and `select_format` may be
  `None`. `Database.codec` is now taken from `Database.driver.codec`
- `Database.db_url` is `None` when a custom driver is given; passing `db_url`, `username` or `password` together
  with `driver` raises a `ValueError`
- **Datetime semantics (major breaking change).** Naive datetimes are now wall-clock times in the column's timezone,
  as in ClickHouse and `clickhouse_driver`, instead of being treated as UTC (see "DateTimeField and Time Zones" in
  the docs):
  - `DateTimeField` / `DateTime64Field` without a `timezone` keep naive values naive on assignment (previously they
    were converted to aware UTC). Fields with a `timezone` localize naive values to it (previously to UTC)
  - Columns without a timezone (e.g. `DateTime`) are now read as naive datetimes in the server's timezone, by
    `select`, querysets and `select_rows` alike and with either driver. Columns with a timezone are read as aware
    values in the column's timezone. The column type decides, not the model field
  - Naive values are written as wall-clock text (`'2020-06-11 04:00:00'`) rather than as a UTC Unix timestamp, in
    inserts, `to_db_string`, function arguments (`toDateTime('...')`) and query parameters, so ClickHouse interprets
    them in the column's timezone. Aware values are still written as Unix timestamps
  - `DateTimeField.to_python` ignores `timezone_in_use`; custom fields still receive the server timezone
  - Aware values (including the default, the Unix epoch) inserted into columns without a timezone are read back as
    naive server wall-clock times
  - To migrate: on a UTC server, replace comparisons with aware UTC values by naive ones (e.g.
    `dt.replace(tzinfo=None)`), or give the field a timezone (e.g. `DateTimeField(timezone='UTC')`, which changes the
    column type to `DateTime('UTC')`) to keep reading aware values. On other servers, check any code which relied
    on naive values meaning UTC

v3.2.0
------
- bugfix: revert changes to session handling

v3.1.0
------
- database now does not use a requests.Session by default
- new `Database.session()` context-manager to recover the old behaviour

v3.0.0
------
- Support up to clickhouse 25.8
- Some complex system tables containing arrays of tuples not yet supported
- Removed iso8601 requirement

**Backwards incompatible changes**

- Dropped support for python < 3.11


v2.2.2
------
- Unpined requirements to enhance compatability

v2.2.1
------
- Minor tooling changes for PyPI
- Better project description for PyPI

v2.2.0
------
- Support up to clickhouse 20.12, including LTS 20.8 release
- Fixed boolean logic for Q objects (https://github.com/Infinidat/infi.clickhouse_orm/issues/158)
- Remove implicit use of '\N' character (which was causing deprecation warnings in queries)
- Tooling updates: use poetry, pytest, isort, black

**Backwards incompatible changes**

You can no longer supply a codec for an `alias` field. Previously this had no effect in clickhouse, but now it explicitly returns an error.

v2.1.3
------
- Fix pagination for models with alias columns

v2.1.2
------
- Add `QuerySet.model` to support django-rest-framework 3

v2.1.1
------
- Improve support of ClickHouse v21.9 (mangototango)
- Ignore non-numeric parts in ClickHouse version (mangototango)
- Fix precedence of ~ operator in Q objects	(mangototango)
- Support for adding a column to the beginning of a table (meanmail)
- Add stddevPop and stddevSamp functions (k.peskov)

v2.1.0
------
- Support for model constraints
- Support for data skipping indexes
- Support for mutations: `QuerySet.update` and `QuerySet.delete`
- Added functions for working with external dictionaries
- Support FINAL for `ReplacingMergeTree` (chripede)
- Added `DateTime64Field` (NiyazNz)
- Make `DateTimeField` and `DateTime64Field` timezone-aware (NiyazNz)

**Backwards incompatible changes**

Previously, `DateTimeField` always converted its value from the database timezone to UTC. This is no longer the case: the field's value now preserves the timezone it was defined with, or if not specified - the database's global timezone. This change has no effect if your database timezone is set to UTC.

v2.0.1
------
- Remove unnecessary import of `six`

v2.0.0
------
- Dropped support for Python 2.x
- New flexible syntax for database expressions and functions
- Expressions as default values for model fields
- Support for IPv4 and IPv6 fields
- Automatic generation of models by inspecting existing tables
- Convenient ways to import ORM classes

See [What's new in version 2](docs/whats_new_in_version_2.md) for details.

v1.4.0
------
- Added primary_key parameter to MergeTree engines (M1hacka)
- Support negative enum values (Romamo)

v1.3.0
------
- Support LowCardinality columns in ad-hoc queries
- Support for LIMIT BY in querysets (utapyngo)

v1.2.0
------
- Add support for per-field compression codecs (rbelio, Chocorean)
- Add support for low cardinality fields (rbelio)

v1.1.0
------
- Add PREWHERE support to querysets (M1hacka)
- Add WITH TOTALS support to querysets (M1hacka)
- Extend date field range (trthhrtz)
- Fix parsing of server errors in ClickHouse v19.3.3+
- Fix pagination when asking for the last page on a query that matches no records
- Use HTTP Basic Authentication instead of passing the credentials in the URL
- Support default/alias/materialized for nullable fields
- Add UUIDField (kpotehin)
- Add `log_statements` parameter to database initializer
- Fix test_merge which fails on ClickHouse v19.8.3
- Fix querysets using the SystemPart model

v1.0.4
------
- Added `timeout` parameter to database initializer (SUHAR1K)
- Added `verify_ssl_cert` parameter to database initializer
- Added `final()` method to querysets (M1hacka)
- Fixed a migrations problem - cannot add a new materialized field after a regular field

v1.0.3
------
- Bug fix: `QuerySet.count()` ignores slicing
- Bug fix: wrong parentheses when building queries using Q objects
- Support Decimal fields
- Added `Database.add_setting` method

v1.0.2
----------
- Include alias and materialized fields in queryset results
- Check for database existence, to allow delayed creation
- Added `Database.does_table_exist` method
- Support for `IS NULL` and `IS NOT NULL` in querysets (kalombos)

v1.0.1
------
- NullableField: take extra_null_values into account in `validate` and `to_python`
- Added `Field.isinstance` method
- Validate the inner field passed to `ArrayField`

v1.0.0
------
- Add support for compound filters with Q objects (desile)
- Add support for BETWEEN operator (desile)
- Distributed engine support (tsionyx)
- `_fields` and `_writable_fields` are OrderedDicts - note that this might break backwards compatibility (tsionyx)
- Improve error messages returned from the database with the `ServerError` class (tsionyx)
- Added support for custom partitioning (M1hacka)
- Added attribute `server_version` to Database class (M1hacka)
- Changed `Engine.create_table_sql()`, `Engine.drop_table_sql()`, `Model.create_table_sql()`, `Model.drop_table_sql()` parameter to db from db_name (M1hacka)
- Fix parsing of datetime column type when it includes a timezone (M1hacka)
- Rename `Model.system` to `Model._system` to prevent collision with a column that has the same name
- Rename `Model.readonly` to `Model._readonly` to prevent collision with a column that has the same name
- The `field_names` argument to `Model.to_tsv` is now mandatory
- Improve creation time of model instances by keeping a dictionary of default values
- Fix queryset bug when field name contains double underscores (YouCanKeepSilence)
- Prevent exception when determining timezone of old ClickHouse versions (vv-p)

v0.9.8
------
- Bug fix: add field names list explicitly to Database.insert method (anci)
- Added RunPython and RunSQL migrations (M1hacka)
- Allow ISO-formatted datetime values (tsionyx)
- Show field name in error message when invalid value assigned (tsionyx)
- Bug fix: select query fails when query contains '$' symbol (M1hacka)
- Prevent problems with AlterTable migrations related to field order (M1hacka)
- Added documentation about custom fields.

v0.9.7
------
- Add `distinct` method to querysets
- Add `AlterTableWithBuffer` migration operation
- Support Merge engine (M1hacka)

v0.9.6
------
- Fix python3 compatibility (TvoroG)
- Nullable arrays not supported in latest ClickHouse version
- system.parts table no longer includes "replicated" column in latest ClickHouse version

v0.9.5
------
- Added `QuerySet.paginate()`
- Support for basic aggregation in querysets

v0.9.4
------
- Migrations: when creating a table for a `BufferModel`, create the underlying table too if necessary

v0.9.3
------
- Changed license from PSF to BSD
- Nullable fields support (yamiou)
- Support for queryset slicing

v0.9.2
------
- Added `ne` and `not_in` queryset operators
- Querysets no longer have a default order unless `order_by` is called
- Added `autocreate` flag to database initializer
- Fix some Python 2/3 incompatibilities (TvoroG, tsionyx)
- To work around a JOIN bug in ClickHouse, `$table` now inserts only the table name,
  and the database name is sent in the query params instead

v0.9.0
------
- Major new feature: building model queries using QuerySets
- Refactor and expand the documentation
- Add support for FixedString fields
- Add support for more engine types: TinyLog, Log, Memory
- Bug fix: Do not send readonly=1 when connection is already in readonly mode

v0.8.2
------
- Fix broken Python 3 support (M1hacka)

v0.8.1
------
- Add support for ReplacingMergeTree (leenr)
- Fix problem with SELECT WITH TOTALS (pilosus)
- Update serialization format of DateTimeField to 10 digits, zero padded (nikepan)
- Greatly improve performance when inserting large strings (credit to M1hacka for identifying the problem)
- Reduce memory footprint of Database.insert()

v0.8.0
------
- Always keep datetime fields in UTC internally, and convert server timezone to UTC when parsing query results
- Support for ALIAS and MATERIALIZED fields (M1ha)
- Pagination: passing -1 as the page number now returns the last page
- Accept datetime values for date fields (Zloool)
- Support readonly mode in Database class (tswr)
- Added support for the Buffer table engine (emakarov)
- Added the SystemPart readonly model, which provides operations on partitions (M1ha)
- Added Model.to_dict() that converts a model instance to a dictionary (M1ha)
- Added Database.raw() to perform arbitrary queries (M1ha)

v0.7.1
------
- Accept '0000-00-00 00:00:00' as a datetime value (tsionyx)
- Bug fix: parse_array fails on int arrays
- Improve performance when inserting many rows

v0.7.0
------
- Support array fields
- Support enum fields

v0.6.3
------
- Python 3 support
