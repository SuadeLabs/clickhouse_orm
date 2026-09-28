"""
An optional backend that builds, executes and materializes queries with SQLAlchemy Core, backed by the
`clickhouse-connect` package's SQLAlchemy dialect (`pip install clickhouse_orm[sqlalchemy]`).

This is purely additive: `Database.select()` / `Model.objects_in()` / the TSV-based `Database.insert()` are
unaffected and remain the default, fast path. This module adds `Database.query()`, which returns model instances
built from `Field.to_python()`, keyed by column name (rather than the position-based TSV decoding used elsewhere).

Table DDL keeps using `Model.create_table_sql()` (see `models.py`), which is executed through the SQLAlchemy engine's
connection rather than reimplemented as SQLAlchemy Core DDL constructs: ClickHouse's engine/partitioning/codec syntax
isn't fully represented by the generic dialect, whereas `create_table_sql()` already covers it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    import sqlalchemy
    from sqlalchemy.engine import URL
    from sqlalchemy.sql import Select
except ImportError as e:  # pragma: no cover - depends on the environment
    raise ImportError("The SQLAlchemy backend requires the sqlalchemy package: pip install clickhouse_orm[sqlalchemy]") from e

try:
    from clickhouse_connect.cc_sqlalchemy.datatypes.sqltypes import sqla_type_from_name
except ImportError as e:  # pragma: no cover - depends on the environment
    raise ImportError(
        "The SQLAlchemy backend requires the clickhouse-connect package: pip install clickhouse_orm[sqlalchemy]"
    ) from e

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .compiler import ServerCapabilities
    from .database import Database
    from .fields import Field
    from .models import Model

#: The SQLAlchemy dialect name registered by clickhouse-connect for its (HTTP-based) driver.
DIALECT_NAME = "clickhousedb"


def field_to_sqla_type(field: Field, capabilities: ServerCapabilities | None = None) -> sqlalchemy.types.TypeEngine:
    """
    Returns the SQLAlchemy (clickhouse-connect) type matching a model field.

    This reuses `Field.get_sql()`, which already produces the field's ClickHouse type name (e.g. `Nullable(String)`,
    `LowCardinality(String)`, `Array(Int32)`, `Enum8('a' = 1, 'b' = 2)`), and hands it to clickhouse-connect's own
    type-name parser - so `Nullable`/`LowCardinality`/`Array`/`Enum` wrapping is handled automatically, with no need
    for a separate, hand-maintained mapping of `Field` subclasses.
    """
    type_name = field.get_sql(with_default_expression=False, capabilities=capabilities)
    return sqla_type_from_name(type_name)


def build_table(
    model_cls: type[Model],
    metadata: sqlalchemy.MetaData,
    schema: str | None = None,
    capabilities: ServerCapabilities | None = None,
) -> sqlalchemy.Table:
    """
    Builds a SQLAlchemy Core `Table` matching a model's fields. Unlike `Database.get_table()`, this always builds a
    new `Table`; it is the caller's responsibility to cache/reuse it (`Table` cannot be added twice to the same
    `MetaData`).

    - `model_cls`: the model to generate a table for.
    - `metadata`: the `MetaData` the table is added to.
    - `schema`: the qualifying database/schema name, if any (e.g. the ORM `Database`'s name, or `"system"`).
    - `capabilities`: a `ServerCapabilities` describing the target server, used the same way as in
      `Model.create_table_sql()` (defaults to a modern server).
    """
    columns = [
        sqlalchemy.Column(name, field_to_sqla_type(field, capabilities))
        for name, field in model_cls.fields().items()
    ]
    return sqlalchemy.Table(model_cls.table_name(), metadata, *columns, schema=schema)


def build_engine(database: Database) -> sqlalchemy.engine.Engine:
    """Builds the lazily-created SQLAlchemy `Engine` used by `Database.engine`."""
    return sqlalchemy.create_engine(build_engine_url(database), future=True)


def build_engine_url(database: Database) -> URL:
    """
    Builds the SQLAlchemy connection URL for a `Database`, reusing its HTTP connection settings
    (`db_url`, and the username/password of its `RequestsDriver`, if any).
    """
    if not database.db_url:
        raise ValueError(
            "The SQLAlchemy backend needs the database's HTTP URL, but this Database was created with a custom "
            "driver instead of db_url/username/password"
        )
    from urllib.parse import urlsplit

    parsed = urlsplit(database.db_url)
    auth = getattr(getattr(database.driver, "session", None), "auth", None) or (None, None)
    username, password = auth
    return URL.create(
        drivername=DIALECT_NAME,
        username=username or None,
        password=password or None,
        host=parsed.hostname,
        port=parsed.port,
        database=database.db_name,
    )


class ModelSelect:
    """
    A SQLAlchemy Core `Select` bound to a `Model` and a `Database`. It wraps a plain `sqlalchemy.sql.Select`
    (accessible as `.statement`), proxying attribute access to it - so it composes normally with `.where()`,
    `.order_by()`, `.join()`, `.limit()`, etc. (each of these returns another `ModelSelect`).

    Iterating over it (or calling `.all()`) executes the statement via `Database.engine` and returns `Model`
    instances built from each result row, matching columns to fields by name (`Field.to_python()`), rather than
    the position-based TSV decoding used by `Database.select()`.
    """

    def __init__(self, statement: Select, database: Database, model_cls: type[Model], table: sqlalchemy.Table):
        self._statement = statement
        self._database = database
        self._model_cls = model_cls
        self.table = table

    @property
    def statement(self) -> Select:
        """The underlying SQLAlchemy `Select` statement."""
        return self._statement

    def __getattr__(self, name: str) -> Any:
        # Proxies to the wrapped Select, so e.g. `.where()`/`.order_by()`/`.limit()`/`.join()` work as expected.
        attr = getattr(self._statement, name)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if isinstance(result, Select):
                return ModelSelect(result, self._database, self._model_cls, self.table)
            return result

        return wrapper

    def all(self) -> list[Model]:
        """Executes the query and returns a list of matching model instances."""
        return list(self)

    def __iter__(self) -> Iterator[Model]:
        with self._database.engine.connect() as conn:
            result = conn.execute(self._statement)
            rows = result.mappings().all()
        for row in rows:
            instance = self._model_cls(**dict(row))
            instance.set_database(self._database)
            yield instance

    def __str__(self) -> str:
        return str(self._statement.compile(compile_kwargs={"literal_binds": True}))


__all__ = ["ModelSelect", "build_engine", "build_engine_url", "build_table", "field_to_sqla_type"]
