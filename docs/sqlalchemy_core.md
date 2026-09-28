SQLAlchemy Core Querying (optional)
====================================

In addition to the regular `QuerySet` API (`Model.objects_in(database)`), clickhouse_orm optionally supports
building, executing and materializing SELECT queries with [SQLAlchemy Core](https://docs.sqlalchemy.org/), via the
`clickhouse-connect` package's SQLAlchemy dialect.

This is entirely additive and opt-in:

* `Database.select()` / `Model.objects_in()` / `QuerySet` (the `Q`/`F` filtering DSL) are unaffected, and remain the
  default query-building path.
* `Database.insert()` (batched TSV over HTTP) remains the default, recommended way to bulk-insert data - it is not
  replaced.
* Table creation (`Database.create_table()`) still uses `Model.create_table_sql()`, which already generates
  ClickHouse-specific DDL (engines, partitioning, codecs, TTL, ...) that a generic SQLAlchemy dialect does not fully
  cover.

What *is* backed by SQLAlchemy Core, when you opt in via `Database.query()`, is:

* **query building**: filtering/ordering/joining use plain SQLAlchemy expressions on a `Table` generated from the
  model, instead of `Q`/`F`;
* **execution**: the query runs through a real SQLAlchemy `Engine` (the `clickhouse-connect` HTTP dialect), instead
  of `clickhouse_orm`'s own `Driver`;
* **row-returning**: each result row is converted into a model instance by matching column names to fields
  (`Field.to_python()`), instead of the position-based TSV parsing used elsewhere.

Installation
------------

    pip install clickhouse_orm[sqlalchemy]

This installs `sqlalchemy` (1.4 or later, including 2.x) and `clickhouse-connect[sqlalchemy]`.

Querying
--------

    from clickhouse_orm.database import Database

    db = Database("mydb")

    qs = db.query(Person)                       # a ModelSelect wrapping a SQLAlchemy Select
    qs = qs.where(qs.table.c.height > 1.7)
    qs = qs.order_by(qs.table.c.last_name)
    qs = qs.limit(10)

    for person in qs:                            # executes via db.engine, yields Person instances
        print(person.first_name, person.height)

`db.query(model_class)` returns a `ModelSelect` (see `clickhouse_orm.alchemy`): it wraps a
`sqlalchemy.sql.Select` (available as `.statement`) and its `.table` attribute is the generated
`sqlalchemy.Table` for `model_class` - so filters are built the normal SQLAlchemy way, e.g.
`qs.table.c.some_field == value`. Any `Select` method that returns another `Select` (`.where()`, `.order_by()`,
`.join()`, `.limit()`, `.group_by()`, ...) returns another `ModelSelect`, so the two APIs compose naturally.

Iterating a `ModelSelect` (or calling `.all()`) executes it and yields `model_class` instances, with
`instance.get_database()` set, just like `Database.select()`.

To select specific columns rather than whole model instances, use `Database.get_table()`/`Database.engine`
directly with plain SQLAlchemy Core:

    from sqlalchemy import select

    table = db.get_table(Person)
    stmt = select(table.c.first_name, table.c.last_name).where(table.c.height > 1.7)
    with db.engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            print(row["first_name"], row["last_name"])

Table metadata
--------------

* `Database.get_table(model_class)` returns (and caches, per `Database` instance) a `sqlalchemy.Table` matching the
  model's fields, qualified with the database name as its schema (or `"system"` for system models).
* `Database.metadata` is the shared `sqlalchemy.MetaData` used for these tables.
* `Database.engine` is a lazily-created SQLAlchemy `Engine`, reusing the `Database`'s HTTP connection settings
  (`db_url`, plus the username/password of its `RequestsDriver`, if any). It requires the database to use the
  default HTTP driver (a custom `driver=` is not supported here).

Field types are mapped to SQLAlchemy/ClickHouse types via `clickhouse_orm.alchemy.field_to_sqla_type()`, which reuses
each field's own `get_sql()` type string (e.g. `Nullable(String)`, `LowCardinality(String)`, `Array(Int32)`,
`Enum8(...)`) and hands it to `clickhouse-connect`'s type-name parser, so wrapped field types are handled the same
way as everywhere else in the ORM.

---

[<< Table of Contents](toc.md) | [Querysets](querysets.md#querysets)
