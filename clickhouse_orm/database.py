from __future__ import annotations

import datetime
import logging
from itertools import chain
from math import ceil

import pytz

from .codec import Codec, TSVCodec
from .compiler import ServerCapabilities, qualified_name, quote_identifier, substitute
from .driver import Driver, RequestsDriver
from .exceptions import DatabaseException, ServerError
from .models import ModelBase
from .utils import Page, import_submodules

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
    ):
        """
        Initializes a database instance. Unless it's readonly, the database will be
        created on the ClickHouse server if it does not already exist.

        - `db_name`: name of the database to connect to.
        - `db_url`: URL of the ClickHouse server.
        - `username`: optional connection credentials.
        - `password`: optional connection credentials.
        - `readonly`: use a read-only connection.
        - `autocreate`: automatically create the database if it does not exist (unless in readonly mode).
        - `timeout`: the connection timeout in seconds.
        - `verify_ssl_cert`: whether to verify the server's certificate when connecting via HTTPS.
        - `log_statements`: when True, all database statements are logged.
        """
        self.db_name = db_name
        self.db_url = db_url or self._default_url
        self.readonly = False
        self.timeout = timeout
        self.driver: Driver = RequestsDriver(
            self.db_url,
            username=username,
            password=password,
            timeout=timeout,
            verify_ssl_cert=verify_ssl_cert,
        )
        self.codec: Codec = TSVCodec()
        self.log_statements = log_statements
        self.settings = {}
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

        fields_list = ",".join(quote_identifier(name) for name in model_class.fields(writable=True))
        fmt = self.codec.insert_format(model_class)
        query = self._substitute("INSERT INTO $table (%s) FORMAT %s" % (fields_list, fmt), model_class)
        instances = self._attach(chain([first_instance], i))
        self._send(query, data=self.codec.encode(model_class, instances, batch_size))

    def count(self, model_class, conditions=None):
        """
        Counts the number of records in the model's table.

        - `model_class`: the model to count.
        - `conditions`: optional SQL conditions (contents of the WHERE clause).
        """
        from clickhouse_orm.query import Q

        query = "SELECT count() FROM $table"
        if conditions:
            if isinstance(conditions, Q):
                conditions = conditions.to_sql(model_class)
            query += " WHERE " + str(conditions)
        query = self._substitute(query, model_class)
        result = self._scalar(query)
        return int(result) if result else 0

    def select(self, query, model_class=None, settings=None):
        """
        Performs a query and returns a generator of model instances.

        - `query`: the SQL query to execute.
        - `model_class`: the model class matching the query's table,
          or `None` for getting back instances of an ad-hoc model.
        - `settings`: query settings to send as HTTP GET parameters
        """
        query += " FORMAT " + self.codec.select_format
        query = self._substitute(query, model_class)
        r = self._send(query, settings=settings, stream=True)
        yield from self._attach(self.codec.decode(r.iter_lines(), model_class, self.server_timezone))

    def select_rows(self, query, settings=None):
        """
        Performs a query and returns a `RowResult`: its `columns` attribute lists the `(name, type)`
        of each column, and iterating over it yields each row as a plain tuple.

        Unlike `select`, no model instances are created. Values use the same Python types as
        `clickhouse_driver` (e.g. `DateTime` columns without a timezone are naive datetimes in the
        server's timezone, and enums are returned as their names). Rows are streamed from the server,
        so the result can only be iterated once.

        - `query`: the SQL query to execute.
        - `settings`: query settings to send as HTTP GET parameters
        """
        query += " FORMAT " + self.codec.select_format
        query = self._substitute(query, None)
        r = self._send(query, settings=settings, stream=True)
        return self.codec.decode_rows(r.iter_lines(), self.server_timezone)

    def raw(self, query, settings=None, stream=False):
        """
        Performs a query and returns its output as text.

        - `query`: the SQL query to execute.
        - `settings`: query settings to send as HTTP GET parameters
        - `stream`: if true, the HTTP response from ClickHouse will be streamed.
        """
        query = self._substitute(query, None)
        return self._send(query, settings=settings, stream=stream).text

    def paginate(self, model_class, order_by, page_num=1, page_size=100, conditions=None, settings=None):
        """
        Selects records and returns a single page of model instances.

        - `model_class`: the model class matching the query's table,
          or `None` for getting back instances of an ad-hoc model.
        - `order_by`: columns to use for sorting the query (contents of the ORDER BY clause).
        - `page_num`: the page number (1-based), or -1 to get the last page.
        - `page_size`: number of records to return per page.
        - `conditions`: optional SQL conditions (contents of the WHERE clause).
        - `settings`: query settings to send as HTTP GET parameters

        The result is a namedtuple containing `objects` (list), `number_of_objects`,
        `pages_total`, `number` (of the current page), and `page_size`.
        """
        from clickhouse_orm.query import Q

        count = self.count(model_class, conditions)
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
            objects=list(self.select(query, model_class, settings)) if count else [],
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

    def _attach(self, instances):
        """Lazily sets this database on each model instance as it is consumed."""
        for instance in instances:
            instance.set_database(self)
            yield instance

    def _send(self, query, data=None, settings=None, stream=False):
        if self.log_statements:
            logger.info(query)
        return self.driver.send(query, data=data, settings=self._build_params(settings), stream=stream)

    def _scalar(self, query, settings=None):
        if self.log_statements:
            logger.info(query)
        return self.driver.scalar(query, settings=self._build_params(settings))

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
