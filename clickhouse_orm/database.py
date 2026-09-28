from __future__ import annotations

import datetime
import logging
from itertools import chain
from math import ceil
from typing import TYPE_CHECKING

import pytz

from .compiler import ServerCapabilities, qualified_name, substitute
from .driver import Driver, RequestsDriver
from .exceptions import DatabaseException, ServerError
from .models import ModelBase
from .params import encode_params
from .utils import Page, import_submodules

if TYPE_CHECKING:
    from .codec import Codec

logger = logging.getLogger("clickhouse_orm")


class Database:
    """
    Database instances connect to a specific ClickHouse database for running queries,
    inserting data and other operations.
    """

    _default_url = "http://localhost:8123/"

    def __init__(
        self,
        db_name,
        db_url=None,
        username=None,
        password=None,
        readonly=False,
        autocreate=True,
        timeout=60,
        verify_ssl_cert=True,
        log_statements=False,
        driver=None,
    ):
        """
        Initializes a database instance. Unless it's readonly, the database will be
        created on the ClickHouse server if it does not already exist.

        - `db_name`: name of the database to connect to.
        - `db_url`: URL of the ClickHouse server's HTTP interface.
        - `username`: optional connection credentials.
        - `password`: optional connection credentials.
        - `readonly`: use a read-only connection.
        - `autocreate`: automatically create the database if it does not exist (unless in readonly mode).
        - `timeout`: the connection timeout in seconds.
        - `verify_ssl_cert`: whether to verify the server's certificate when connecting via HTTPS.
        - `log_statements`: when True, all database statements are logged.
        - `driver`: the `Driver` used to communicate with the server. Defaults to a `RequestsDriver` configured by
          `db_url`, `username`, `password`, `timeout` and `verify_ssl_cert`, which only apply to the default driver.
        """
        self.db_name = db_name
        self.readonly = False
        self.timeout = timeout
        if driver is None:
            self.db_url = db_url or self._default_url
            driver = RequestsDriver(
                self.db_url,
                username=username,
                password=password,
                timeout=timeout,
                verify_ssl_cert=verify_ssl_cert,
            )
        elif db_url or username or password:
            raise ValueError(
                "db_url, username and password configure the default driver, and cannot be used with driver"
            )
        else:
            self.db_url = None
        self.driver: Driver = driver
        self.codec: Codec = driver.codec
        self.log_statements = log_statements
        self.settings = {}
        # Lazily created by the optional SQLAlchemy backend (see `engine`, `metadata`, `get_table()`, `query()`)
        self._sa_engine = None
        self._sa_metadata = None
        self._sa_tables = {}
        self.db_exists = False  # this is required before running _is_existing_database
        self.db_exists = self._is_existing_database()
        if readonly:
            if not self.db_exists:
                raise DatabaseException("Database does not exist, and cannot be created under readonly connection")
            self.connection_readonly = self._is_connection_readonly()
            self.readonly = True
        elif autocreate and not self.db_exists:
            self.create_database()
        self.server_version = self._get_server_version()
        # Versions 1.1.53981 and below don't have timezone function
        self.server_timezone = self._get_server_timezone() if self.server_version > (1, 1, 53981) else pytz.utc
        self.capabilities = ServerCapabilities.from_version(self.server_version)

    @property
    def has_codec_support(self):
        """Whether the server supports column compression codecs (19.1.16+)."""
        return self.capabilities.has_codec_support

    @property
    def has_low_cardinality_support(self):
        """Whether the server supports LowCardinality columns (19.0+)."""
        return self.capabilities.has_low_cardinality_support

    def create_database(self):
        """
        Creates the database on the ClickHouse server if it does not already exist.
        """
        self._send("CREATE DATABASE IF NOT EXISTS `%s`" % self.db_name)
        self.db_exists = True

    def drop_database(self):
        """
        Deletes the database on the ClickHouse server.
        """
        self._send("DROP DATABASE `%s`" % self.db_name)
        self.db_exists = False

    def create_table(self, model_class):
        """
        Creates a table for the given model class, if it does not exist already.
        """
        if model_class.is_system_model():
            raise DatabaseException("You can't create system table")
        if model_class.engine is None:
            raise DatabaseException("%s class must define an engine" % model_class.__name__)
        self._send(model_class.create_table_sql(self.db_name, self.capabilities))

    def drop_table(self, model_class):
        """
        Drops the database table of the given model class, if it exists.
        """
        if model_class.is_system_model():
            raise DatabaseException("You can't drop system table")
        self._send(model_class.drop_table_sql(self.db_name))

    @property
    def engine(self):
        """
        A lazily-created SQLAlchemy `Engine`, connected via the `clickhouse-connect` SQLAlchemy dialect and reusing
        this database's HTTP connection settings. Used by `query()` (see `clickhouse_orm.alchemy`).

        Requires the `sqlalchemy` extra (`pip install clickhouse_orm[sqlalchemy]`).
        """
        if self._sa_engine is None:
            from .alchemy import build_engine

            self._sa_engine = build_engine(self)
        return self._sa_engine

    @property
    def metadata(self):
        """The shared SQLAlchemy `MetaData` used by `get_table()`/`query()`."""
        if self._sa_metadata is None:
            from .alchemy import sqlalchemy

            self._sa_metadata = sqlalchemy.MetaData()
        return self._sa_metadata

    def get_table(self, model_class):
        """
        Returns the SQLAlchemy Core `Table` matching a model class, building (and caching, on this `Database`
        instance) it if necessary. See `clickhouse_orm.alchemy.build_table()`.
        """
        table = self._sa_tables.get(model_class)
        if table is None:
            from .alchemy import build_table

            schema = "system" if model_class.is_system_model() else self.db_name
            table = build_table(model_class, self.metadata, schema=schema, capabilities=self.capabilities)
            self._sa_tables[model_class] = table
        return table

    def query(self, model_class, *entities):
        """
        Returns a `ModelSelect`, a SQLAlchemy Core `Select` bound to `model_class`'s table, as an alternative to
        `model_class.objects_in(self)`. Unlike a `QuerySet`, it is filtered/ordered/joined using plain SQLAlchemy
        expressions on `Table.c` (e.g. `qs.table.c.value > 10`) rather than `Q`/`funcs`.

        - `model_class`: the model to query; its table is available as `.table` on the returned `ModelSelect`.
        - `entities`: optional specific columns/expressions to select, instead of the whole table.

        Requires the `sqlalchemy` extra (`pip install clickhouse_orm[sqlalchemy]`).
        """
        from .alchemy import ModelSelect, sqlalchemy

        table = self.get_table(model_class)
        statement = sqlalchemy.select(*entities) if entities else sqlalchemy.select(table)
        return ModelSelect(statement, self, model_class, table)

    def does_table_exist(self, model_class):
        """
        Checks whether a table for the given model class already exists.
        Note that this only checks for existence of a table with the expected name.
        """
        sql = "SELECT count() FROM system.tables WHERE database = '%s' AND name = '%s'"
        return self._scalar(sql % (self.db_name, model_class.table_name())) == "1"

    def get_model_for_table(self, table_name, system_table=False):
        """
        Generates a model class from an existing table in the database.
        This can be used for querying tables which don't have a corresponding model class,
        for example system tables.

        - `table_name`: the table to create a model for
        - `system_table`: whether the table is a system table, or belongs to the current database
        """
        db_name = "system" if system_table else self.db_name
        sql = "DESCRIBE %s" % qualified_name(db_name, table_name)
        fields = [(column.name, column.type) for column in self.select(sql)]
        model = ModelBase.create_ad_hoc_model(fields, table_name)
        if system_table:
            model._system = model._readonly = True
        return model

    def add_setting(self, name, value):
        """
        Adds a database setting that will be sent with every request.
        For example, `db.add_setting("max_execution_time", 10)` will
        limit query execution time to 10 seconds.
        The name must be string, and the value is converted to string in case
        it isn't. To remove a setting, pass `None` as the value.
        """
        assert isinstance(name, str), "Setting name must be a string"
        if value is None:
            self.settings.pop(name, None)
        else:
            self.settings[name] = str(value)

    def insert(self, model_instances, batch_size=1000):
        """
        Insert records into the database.

        - `model_instances`: any iterable containing instances of a single model class.
        - `batch_size`: number of records to send per chunk (use a lower number if your records are very large).
        """
        i = iter(model_instances)
        try:
            first_instance = next(i)
        except StopIteration:
            return  # model_instances is empty
        model_class = first_instance.__class__

        if first_instance.is_read_only() or first_instance.is_system_model():
            raise DatabaseException("You can't insert into read only and system tables")

        instances = self._attach(chain([first_instance], i))
        for statement, data in self.codec.encode_inserts(model_class, instances, batch_size):
            self._send(self._substitute(statement, model_class), data=data)

    def count(self, model_class, conditions=None, params=None):
        """
        Counts the number of records in the model's table.

        - `model_class`: the model to count.
        - `conditions`: optional SQL conditions (contents of the WHERE clause).
        - `params`: values for `{name:Type}` placeholders in the conditions.
        """
        from clickhouse_orm.query import Q

        query = "SELECT count() FROM $table"
        if conditions:
            if isinstance(conditions, Q):
                conditions = conditions.to_sql(model_class)
            query += " WHERE " + str(conditions)
        query = self._substitute(query, model_class)
        result = self._scalar(query, params=params)
        return int(result) if result else 0

    def select(self, query, model_class=None, settings=None, params=None):
        """
        Performs a query and returns a generator of model instances.

        - `query`: the SQL query to execute.
        - `model_class`: the model class matching the query's table,
          or `None` for getting back instances of an ad-hoc model.
        - `settings`: query settings to send as HTTP GET parameters
        - `params`: values for `{name:Type}` placeholders in the query (see "Query Parameters")
        """
        query = self._substitute(self._with_select_format(query), model_class)
        r = self._send(query, settings=settings, stream=True, params=params)
        yield from self._attach(self.codec.decode(r, model_class, self.server_timezone))

    def select_rows(self, query, settings=None, params=None):
        """
        Performs a query and returns a `RowResult`: its `columns` attribute lists the `(name, type)`
        of each column, and iterating over it yields each row as a plain tuple.

        Unlike `select`, no model instances are created. Values use the same Python types as
        `clickhouse_driver` (e.g. `DateTime` columns without a timezone are naive datetimes in the
        server's timezone, and enums are returned as their names). Rows are streamed from the server,
        so the result can only be iterated once.

        - `query`: the SQL query to execute.
        - `settings`: query settings to send as HTTP GET parameters
        - `params`: values for `{name:Type}` placeholders in the query
        """
        query = self._substitute(self._with_select_format(query), None)
        r = self._send(query, settings=settings, stream=True, params=params)
        return self.codec.decode_rows(r, self.server_timezone)

    def raw(self, query, settings=None, stream=False, params=None):
        """
        Performs a query and returns its output as text.

        - `query`: the SQL query to execute.
        - `settings`: query settings to send as HTTP GET parameters
        - `stream`: if true, the HTTP response from ClickHouse will be streamed.
        - `params`: values for `{name:Type}` placeholders in the query
        """
        query = self._substitute(query, None)
        return self._send(query, settings=settings, stream=stream, params=params).text

    def paginate(self, model_class, order_by, page_num=1, page_size=100, conditions=None, settings=None, params=None):
        """
        Selects records and returns a single page of model instances.

        - `model_class`: the model class matching the query's table,
          or `None` for getting back instances of an ad-hoc model.
        - `order_by`: columns to use for sorting the query (contents of the ORDER BY clause).
        - `page_num`: the page number (1-based), or -1 to get the last page.
        - `page_size`: number of records to return per page.
        - `conditions`: optional SQL conditions (contents of the WHERE clause).
        - `settings`: query settings to send as HTTP GET parameters
        - `params`: values for `{name:Type}` placeholders in the conditions

        The result is a namedtuple containing `objects` (list), `number_of_objects`,
        `pages_total`, `number` (of the current page), and `page_size`.
        """
        from clickhouse_orm.query import Q

        count = self.count(model_class, conditions, params=params)
        pages_total = int(ceil(count / float(page_size)))
        if page_num == -1:
            page_num = max(pages_total, 1)
        elif page_num < 1:
            raise ValueError("Invalid page number: %d" % page_num)
        offset = (page_num - 1) * page_size
        query = "SELECT * FROM $table"
        if conditions:
            if isinstance(conditions, Q):
                conditions = conditions.to_sql(model_class)
            query += " WHERE " + str(conditions)
        query += " ORDER BY %s" % order_by
        query += " LIMIT %d, %d" % (offset, page_size)
        query = self._substitute(query, model_class)
        return Page(
            objects=list(self.select(query, model_class, settings, params=params)) if count else [],
            number_of_objects=count,
            pages_total=pages_total,
            number=page_num,
            page_size=page_size,
        )

    def migrate(self, migrations_package_name, up_to=9999):
        """
        Executes schema migrations.

        - `migrations_package_name` - fully qualified name of the Python package
          containing the migrations.
        - `up_to` - number of the last migration to apply.
        """
        from .migrations import MigrationHistory

        logger = logging.getLogger("migrations")
        applied_migrations = self._get_applied_migrations(migrations_package_name)
        modules = import_submodules(migrations_package_name)
        unapplied_migrations = set(modules.keys()) - applied_migrations
        for name in sorted(unapplied_migrations):
            logger.info("Applying migration %s...", name)
            for operation in modules[name].operations:
                operation.apply(self)
            self.insert(
                [
                    MigrationHistory(
                        package_name=migrations_package_name, module_name=name, applied=datetime.date.today()
                    )
                ]
            )
            if int(name[:4]) >= up_to:
                break

    def _get_applied_migrations(self, migrations_package_name):
        from .migrations import MigrationHistory

        self.create_table(MigrationHistory)
        query = "SELECT module_name from $table WHERE package_name = '%s'" % migrations_package_name
        query = self._substitute(query, MigrationHistory)
        return set(obj.module_name for obj in self.select(query))

    @property
    def request_session(self):
        """The `requests.Session` used by the default `RequestsDriver`. Kept for backwards compatibility."""
        return self.driver.session

    def _with_select_format(self, query):
        return query + " FORMAT " + self.codec.select_format if self.codec.select_format else query

    def _attach(self, instances):
        """Lazily sets this database on each model instance as it is consumed."""
        for instance in instances:
            instance.set_database(self)
            yield instance

    def _send(self, query, data=None, settings=None, stream=False, params=None):
        params = self._encode_params(query, params)
        return self.driver.send(query, data=data, settings=self._build_params(settings), stream=stream, params=params)

    def _scalar(self, query, settings=None, params=None):
        params = self._encode_params(query, params)
        return self.driver.scalar(query, settings=self._build_params(settings), params=params)

    def _encode_params(self, query, params):
        encoded = encode_params(params)
        if self.log_statements:
            logger.info(query)
            if encoded:
                logger.info("params: %s", encoded)
        return encoded

    def _build_params(self, settings):
        params = dict(settings or {})
        params.update(self.settings)
        if self.db_exists:
            params["database"] = self.db_name
        # Send the readonly flag, unless the connection is already readonly (to prevent db error)
        if self.readonly and not self.connection_readonly:
            params["readonly"] = "1"
        return params

    def _substitute(self, query, model_class=None):
        """
        Replaces $db and $table placeholders in the query.
        """
        return substitute(query, self.db_name, model_class)

    def _get_server_timezone(self):
        try:
            return pytz.timezone(self._scalar("SELECT timezone()"))
        except ServerError as e:
            logger.exception("Cannot determine server timezone (%s), assuming UTC", e)
            return pytz.utc

    def _get_server_version(self, as_tuple=True):
        try:
            ver = self._scalar("SELECT version();")
        except ServerError as e:
            logger.exception("Cannot determine server version (%s), assuming 1.1.0", e)
            ver = "1.1.0"
        return tuple(int(n) for n in ver.split(".") if n.isdigit()) if as_tuple else ver

    def _is_existing_database(self):
        return self._scalar("SELECT count() FROM system.databases WHERE name = '%s'" % self.db_name) == "1"

    def _is_connection_readonly(self):
        return self._scalar("SELECT value FROM system.settings WHERE name = 'readonly'") != "0"


# Expose only relevant classes in import *
__all__ = [c.__name__ for c in [Page, DatabaseException, ServerError, Database]]
