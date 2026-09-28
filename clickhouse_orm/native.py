"""
A driver using ClickHouse's native TCP protocol, backed by the optional `clickhouse-driver` package
(`pip install clickhouse_orm[native]`).
"""

from __future__ import annotations

import codecs
import datetime
import math
import struct
from decimal import Decimal
from itertools import groupby
from typing import TYPE_CHECKING, Any

import pytz

try:
    from clickhouse_driver import Client
    from clickhouse_driver.defines import DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS
    from clickhouse_driver.errors import ServerException
    from clickhouse_driver.util.helpers import parse_url
except ImportError as e:  # pragma: no cover - depends on the environment
    raise ImportError("NativeDriver requires the clickhouse-driver package: pip install clickhouse_orm[native]") from e

from . import fields as orm_fields
from .codec import Codec, RowResult
from .compiler import quote_identifier
from .driver import Driver
from .exceptions import DatabaseException, ServerError
from .models import ModelBase
from .utils import NO_VALUE, escape, parse_tuple_type, unescape

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from .codec import Column
    from .fields import Field
    from .models import Model

#: Field classes defined by the ORM. Any other field class is a custom field.
_BUILTIN_FIELDS = frozenset(
    cls for cls in vars(orm_fields).values() if isinstance(cls, type) and issubclass(cls, orm_fields.Field)
)
_WRAPPER_FIELDS = (orm_fields.NullableField, orm_fields.LowCardinalityField, orm_fields.ArrayField)


class NativeInsertData:
    """
    The insert data produced by `NativeCodec`: an iterator over the rows (tuples of Python values), sent by
    `NativeDriver` in blocks of `block_size` rows so that inserting from an iterator uses bounded memory.
    """

    def __init__(self, rows: Iterator[tuple], block_size: int):
        self.rows = rows
        self.block_size = block_size

    def __iter__(self) -> NativeInsertData:
        return self

    def __next__(self) -> tuple:
        return next(self.rows)


class NativeResponse:
    """
    The response of `NativeDriver.send`: the result `columns` as `(name, type)` pairs, and the `rows` as tuples.

    `text` renders the rows in the TabSeparated format, for `Database.raw` and `Driver.scalar`. It approximates
    the server's own formatting, which the native protocol does not provide.
    """

    def __init__(self, columns: list[Column], rows: list[tuple[Any, ...]]):
        self.columns = columns
        self.rows = rows

    @property
    def text(self) -> str:
        return "".join(line.decode("utf-8") + "\n" for line in self.iter_lines())

    def iter_lines(self) -> Iterator[bytes]:
        for row in self.rows:
            yield "\t".join(_format_text(value) for value in row).encode("utf-8")


class NativeCodec(Codec):
    """
    The codec of `NativeDriver`. Values are exchanged as Python objects, which `clickhouse_driver` converts to and
    from ClickHouse's binary Native format, so only values whose Python representation differs from the model's
    are converted: enums, NULL-like values and custom fields. Datetimes are returned by `clickhouse_driver` like
    the ORM represents them: naive for columns without a timezone, and aware in the column's timezone otherwise.
    """

    select_format = None

    def encode_inserts(self, model_class, instances, batch_size=1000):
        fields = model_class.fields(writable=True)
        encoders = {name: _value_encoder(field) for name, field in fields.items()}
        if not model_class.has_funcs_as_defaults():
            yield self._statement(fields), NativeInsertData(self._rows(instances, list(encoders.items())), batch_size)
            return
        # Omit the fields without a value, so that ClickHouse evaluates their defaults. The fields must be the same
        # in all the rows of a statement, so consecutive instances with the same fields are grouped together.
        for names, group in groupby(instances, key=lambda instance: _assigned_fields(instance, fields)):
            rows = self._rows(group, [(name, encoders[name]) for name in names])
            yield self._statement(names), NativeInsertData(rows, batch_size)

    @staticmethod
    def _statement(names: Iterable[str]) -> str:
        return "INSERT INTO $table (%s) VALUES" % ",".join(quote_identifier(name) for name in names)

    @staticmethod
    def _rows(instances, encoders):
        for instance in instances:
            data = instance.__dict__
            yield tuple(encode(data[name]) if encode else data[name] for name, encode in encoders)

    def decode(self, response, model_class=None, timezone=pytz.utc):
        columns = response.columns
        model_class = model_class or ModelBase.create_ad_hoc_model(columns)
        names = [name for name, _ in columns]
        decoders = [_value_decoder(getattr(model_class, name), timezone) for name in names]
        for row in response.rows:
            yield model_class(
                **{name: decode(value) if decode else value for name, decode, value in zip(names, decoders, row)}
            )

    def decode_rows(self, response, timezone=pytz.utc):
        return RowResult(response.columns, iter(response.rows))


class NativeDriver(Driver):
    """
    A ClickHouse driver using the native TCP protocol, via `clickhouse_driver.Client`.

    Results are read in full before they are returned (`stream` is ignored), which allows running other queries while
    iterating over them. Like `clickhouse_driver.Client`, a driver must not be used by several threads at once.
    """

    codec = NativeCodec()

    def __init__(self, host: str = "localhost", **client_kwargs: Any):
        """
        - `host`: the server's hostname.
        - `client_kwargs`: other arguments of `clickhouse_driver.Client`, such as `port`, `user`, `password`,
          `secure` or `settings`. The database is chosen by `Database`, so `database` cannot be given.
        """
        if "database" in client_kwargs:
            raise ValueError("The database is chosen by Database, and cannot be given to NativeDriver")
        settings = dict(client_kwargs.pop("settings", None) or {})
        # Placeholders are rendered by the server, as with the HTTP interface
        settings["server_side_params"] = True
        self.host = host
        self.client_kwargs = {**client_kwargs, "settings": settings}
        self._clients: dict[str | None, Client] = {}

    @classmethod
    def from_url(cls, url: str) -> NativeDriver:
        """
        Creates a driver from a URL, e.g. `clickhouse://user:password@localhost:9000`.
        See `clickhouse_driver.Client.from_url` for the supported URLs. Any database in the URL is ignored.
        """
        host, kwargs = parse_url(url)
        kwargs.pop("database", None)
        return cls(host, **kwargs)

    def client(self, database: str | None = None) -> Client:
        """Returns the `clickhouse_driver.Client` connected to `database`, or to the user's default database."""
        # The native protocol selects the database when connecting, so each database has its own client
        if database not in self._clients:
            kwargs = self.client_kwargs if database is None else {**self.client_kwargs, "database": database}
            self._clients[database] = Client(self.host, **kwargs)
        return self._clients[database]

    def disconnect(self) -> None:
        """Closes all connections to the server."""
        for client in self._clients.values():
            client.disconnect()
        self._clients.clear()

    def send(self, query, data=None, settings=None, stream=False, params=None):
        settings = dict(settings or {})
        client = self.client(settings.pop("database", None))
        server_params = {name: _Param(value) for name, value in params.items()} if params else None
        try:
            if data is not None:
                if isinstance(data, NativeInsertData):
                    settings["insert_block_size"] = data.block_size
                # clickhouse_driver treats generators (but not other iterables) as insert data
                client.execute(query, (row for row in data), settings=settings)
                return NativeResponse([], [])
            rows, columns = client.execute(query, server_params, with_column_types=True, settings=settings)
        except ServerException as e:
            if server_params and e.code == _UNKNOWN_QUERY_PARAMETER:
                _check_params_supported(client)
            raise ServerError("Code: %d. %s" % (e.code, e.message))
        return NativeResponse(columns, _normalize_rows(columns, rows))


def _check_params_supported(client: Client) -> None:
    """Raises a `DatabaseException` if the server does not support query parameters over the native protocol."""
    # The client disconnects after an error, so reconnect to find out the server's protocol revision
    client.execute("SELECT 1")
    server_info = client.connection.server_info
    if server_info.used_revision < DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS:
        version = ".".join(map(str, server_info.version_tuple()))
        raise DatabaseException("ClickHouse %s does not support query parameters over the native protocol" % version)


_UNKNOWN_QUERY_PARAMETER = 456


class _Param:
    """
    A query parameter already encoded in the escaped text format (see `clickhouse_orm.params.format_param`).
    The native protocol sends parameters as quoted strings: `clickhouse_driver` quotes objects which are not
    strings using their `str()`, which escapes the value for the quotes.
    """

    def __init__(self, text: str):
        self.text = text

    def __str__(self):
        return self.text.replace("\\", "\\\\").replace("'", "\\'")


def _assigned_fields(instance: Model, fields: Mapping[str, Field]) -> tuple[str, ...]:
    return tuple(name for name in fields if instance.__dict__[name] != NO_VALUE)


def _is_builtin(field: Field) -> bool:
    return type(field) in _BUILTIN_FIELDS


def _value_encoder(field: Field) -> Callable[[Any], Any] | None:
    """
    Returns a function converting a model value of `field` to the Python value expected by `clickhouse_driver`,
    or `None` when the value can be sent as is.
    """
    if not _is_builtin(field):
        return _custom_value_encoder(field)
    if isinstance(field, orm_fields.BaseEnumField):
        return lambda value: value.name
    if isinstance(field, orm_fields.LowCardinalityField):
        return _value_encoder(field.inner_field)
    if isinstance(field, orm_fields.ArrayField):
        inner = _value_encoder(field.inner_field)
        return (lambda value: [inner(item) for item in value]) if inner else None
    if isinstance(field, orm_fields.TupleField):
        encoders = [_value_encoder(inner_field) for inner_field in field.inner_fields]
        if not any(encoders):
            return None
        return lambda value: tuple(encode(item) if encode else item for encode, item in zip(encoders, value))
    if isinstance(field, orm_fields.NullableField):
        inner = _value_encoder(field.inner_field)
        null_values = field._null_values
        if inner is None and null_values == [None]:
            return None
        return lambda value: None if value in null_values else (inner(value) if inner else value)
    return None


def _custom_value_encoder(field: Field) -> Callable[[Any], Any]:
    """
    Custom fields only define how their values are written in the text formats (`to_db_string`). The text is parsed
    by the field for the column's type, exactly as the server parses it when inserting in the TabSeparated format.
    """
    try:
        column_field = ModelBase.create_ad_hoc_field(field._get_sql(False, None))
    except (NotImplementedError, AssertionError):
        return lambda value: value
    column_encoder = _value_encoder(column_field)

    def encode(value):
        text = field.to_db_string(value, quote=False)
        value = None if text == "\\N" else column_field.to_python(unescape(text), pytz.utc)
        return column_encoder(value) if column_encoder else value

    return encode


def _needs_to_python(field: Field) -> bool:
    """Whether the values of `field` returned by `clickhouse_driver` must be converted by `to_python` with a timezone."""
    if isinstance(field, _WRAPPER_FIELDS) and _is_builtin(field):
        return _needs_to_python(field.inner_field)
    if isinstance(field, orm_fields.TupleField) and _is_builtin(field):
        return any(_needs_to_python(inner_field) for inner_field in field.inner_fields)
    return not _is_builtin(field)


def _value_decoder(field: Field, timezone: datetime.tzinfo) -> Callable[[Any], Any] | None:
    """
    Returns a function converting a value returned by `clickhouse_driver` for `field`, before it is assigned to the
    model, or `None` when assignment suffices. Custom fields receive the server's timezone, as with the TSV codec.
    """
    if not _needs_to_python(field):
        return None
    return lambda value: field.to_python(value, timezone)


def _normalize_rows(columns: list[Column], rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    normalizers = [_value_normalizer(db_type) for _, db_type in columns]
    if not any(normalizers):
        return rows
    return [
        tuple(
            normalize(value) if normalize and value is not None else value for normalize, value in zip(normalizers, row)
        )
        for row in rows
    ]


def _value_normalizer(db_type: str) -> Callable[[Any], Any] | None:
    """
    Returns a function converting a non-NULL value of `db_type` returned by `clickhouse_driver` to the value read from
    the text formats, if they differ. This is the case for `Float32`, which `clickhouse_driver` widens to the nearest
    double (e.g. 1.7000000476837158), while the server writes the shortest decimal representation (1.7).
    """
    for wrapper in ("Nullable(", "LowCardinality("):
        if db_type.startswith(wrapper):
            return _value_normalizer(db_type[len(wrapper) : -1])
    if db_type.startswith("Array("):
        inner = _value_normalizer(db_type[6:-1])
        return (lambda value: [None if item is None else inner(item) for item in value]) if inner else None
    if db_type.startswith("Tuple("):
        normalizers = [_value_normalizer(element_type) for _, element_type in parse_tuple_type(db_type)]
        if not any(normalizers):
            return None
        return lambda value: tuple(
            normalize(item) if normalize and item is not None else item for normalize, item in zip(normalizers, value)
        )
    return _shortest_float32 if db_type == "Float32" else None


_FLOAT32 = struct.Struct("f")


def _shortest_float32(value: float) -> float:
    if not math.isfinite(value):
        return value
    for digits in range(1, 10):
        candidate = float("%.*g" % (digits, value))
        if _FLOAT32.unpack(_FLOAT32.pack(candidate))[0] == value:
            return candidate
    return value


def _format_text(value: Any) -> str:
    """Formats a value returned by `clickhouse_driver` like the server does in the TabSeparated format."""
    return "\\N" if value is None else _format_value(value, quote=False)


def _format_value(value: Any, quote: bool = True) -> str:
    """Formats a value, quoting strings when `quote` is true (inside arrays, tuples and maps)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, list):
        return "[%s]" % ",".join(_format_value(item) for item in value)
    if isinstance(value, tuple):
        return "(%s)" % ",".join(_format_value(item) for item in value)
    if isinstance(value, dict):
        return "{%s}" % ",".join("%s:%s" % (_format_value(k), _format_value(v)) for k, v in value.items())
    if isinstance(value, datetime.datetime):
        # Datetimes are naive in the server's timezone, or aware in the column's timezone
        value = value.replace(tzinfo=None).isoformat(" ")
    elif not isinstance(value, (str, bytes)):
        value = str(value)
    return "'%s'" % _escape(value) if quote else _escape(value)


def _escape(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return codecs.escape_encode(value)[0].decode("ascii")
    return escape(value, quote=False)


__all__ = ["NativeCodec", "NativeDriver", "NativeInsertData", "NativeResponse"]
