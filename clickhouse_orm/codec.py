"""A codec converts model instances to and from the wire format used to exchange data with ClickHouse."""

from __future__ import annotations

import abc
import codecs
import datetime
import re
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytz

from .compiler import quote_identifier
from .fields import ArrayField, BaseEnumField
from .models import Model, ModelBase
from .utils import parse_array, parse_tsv, unescape

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

#: A column description: its name and ClickHouse type, e.g. `("id", "UInt64")`.
Column = tuple[str, str]


class RowResult:
    """
    The result of a query whose rows are returned as plain tuples rather than model instances.

    The layout matches `clickhouse_driver.Client.execute(..., with_column_types=True)`:
    `columns` is a list of `(name, type)` pairs, and iterating yields one tuple per row.
    Rows are streamed from the server, so the result can only be iterated once.
    """

    def __init__(self, columns: list[Column], rows: Iterator[tuple[Any, ...]]):
        self.columns = columns
        self._rows = rows

    @property
    def column_names(self) -> list[str]:
        """The names of the result columns, in order. Names are not necessarily unique."""
        return [name for name, _ in self.columns]

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return self._rows

    def __repr__(self):
        return f"<{self.__class__.__name__} columns={self.columns!r}>"


class Codec(abc.ABC):
    """
    Base class for codecs. A codec is paired with a driver (see `Driver.codec`): it builds the statements whose data
    it encodes, and decodes the responses returned by the driver's `send`.
    """

    #: The ClickHouse format name appended to SELECT queries in a `FORMAT` clause, or `None` to send queries as is.
    select_format: str | None

    @abc.abstractmethod
    def encode_inserts(
        self, model_class: type[Model], instances: Iterable[Model], batch_size: int = 1000
    ) -> Iterator[tuple[str, Any]]:
        """
        Serialises model instances for insertion. Yields `(statement, data)` pairs, each to be sent to the driver
        as `send(statement, data=data)`. Statements use the `$table` placeholder for the model's table.

        - `model_class`: the model class of all the instances.
        - `instances`: the instances to serialise.
        - `batch_size`: the maximum number of instances per chunk of data, for codecs which send data in chunks.
        """

    @abc.abstractmethod
    def decode(
        self,
        response: Any,
        model_class: type[Model] | None = None,
        timezone: datetime.tzinfo = pytz.utc,
    ) -> Iterator[Model]:
        """
        Deserialises the response to a SELECT query into model instances.

        - `response`: the response returned by the driver's `send`.
        - `model_class`: the model class matching the query's columns,
          or `None` for getting back instances of an ad-hoc model.
        - `timezone`: the server's timezone, passed to the `to_python` of custom fields.

        Datetimes are converted according to their column's type, as in `decode_rows`, before being assigned.
        """

    @abc.abstractmethod
    def decode_rows(self, response: Any, timezone: datetime.tzinfo = pytz.utc) -> RowResult:
        """
        Deserialises the response to a SELECT query into a `RowResult`.

        Values use the same Python types as `clickhouse_driver`, so that results do not depend on the driver.
        In particular, `DateTime` columns without an explicit timezone are returned as naive datetimes (wall-clock
        times in the server's timezone), columns with a timezone as aware datetimes in that timezone, and enums
        as their names.

        - `response`: the response returned by the driver's `send`.
        - `timezone`: the server's timezone.
        """


class TSVCodec(Codec):
    """
    A codec using ClickHouse's tab-separated formats: `TabSeparatedWithNamesAndTypes` for reading,
    and `TabSeparated` (or `TSKV` for models with function expressions as defaults) for writing.
    Responses must provide `iter_lines()` (see `DriverResponse`), and insert data is sent as chunks of bytes.
    """

    select_format = "TabSeparatedWithNamesAndTypes"

    def insert_format(self, model_class: type[Model]) -> str:
        """Returns the ClickHouse format name used when inserting `model_class` instances."""
        # TSKV lets ClickHouse evaluate defaults for fields omitted from the row
        return "TSKV" if model_class.has_funcs_as_defaults() else "TabSeparated"

    def encode_inserts(self, model_class, instances, batch_size=1000):
        fields_list = ",".join(quote_identifier(name) for name in model_class.fields(writable=True))
        statement = "INSERT INTO $table (%s) FORMAT %s" % (fields_list, self.insert_format(model_class))
        yield statement, self.encode(model_class, instances, batch_size)

    def encode(self, model_class: type[Model], instances: Iterable[Model], batch_size: int = 1000) -> Iterator[bytes]:
        """Serialises model instances into chunks of at most `batch_size` lines in the model's insert format."""
        buf = BytesIO()
        lines = 0
        for instance in instances:
            buf.write(instance.to_db_string())
            lines += 1
            if lines >= batch_size:
                yield buf.getvalue()
                buf = BytesIO()
                lines = 0
        if lines:
            yield buf.getvalue()

    def decode(self, response, model_class=None, timezone=pytz.utc):
        lines = response.iter_lines()
        field_names = parse_tsv(next(lines))
        field_types = parse_tsv(next(lines))
        model_class = model_class or ModelBase.create_ad_hoc_model(zip(field_names, field_types))
        parsers = [_cell_parser(db_type, timezone) for db_type in field_types]
        for line in lines:
            # skip blank line left by WITH TOTALS modifier
            if line:
                cells = line.split(b"\t")
                yield model_class(**{name: parse(cell) for name, parse, cell in zip(field_names, parsers, cells)})

    def decode_rows(self, response, timezone=pytz.utc):
        lines = response.iter_lines()
        columns = list(zip(parse_tsv(next(lines)), parse_tsv(next(lines))))
        converters = [(_row_converter(db_type, timezone), _is_quoted_type(db_type)) for _, db_type in columns]
        return RowResult(columns, self._iter_rows(lines, converters))

    @staticmethod
    def _iter_rows(lines, converters):
        for line in lines:
            # skip blank line left by WITH TOTALS modifier
            if line:
                yield tuple(
                    None if value == b"\\N" else convert(value if quoted else codecs.escape_decode(value)[0])
                    for (convert, quoted), value in zip(converters, line.split(b"\t"))
                )


# Types the ORM cannot parse yet are returned as their ClickHouse text representation
_TEXT_TYPE_PREFIXES = ("Tuple(", "Map(", "Nested(", "Variant(", "Dynamic", "JSON", "Object(", "AggregateFunction(")
_DATETIME_TYPE = re.compile(r"\bDateTime(64)?\b")
_QUOTED_STRING = re.compile(r"'(?:[^'\\]|\\.)*'")
_QUOTED_TYPE_PREFIXES = ("Array(", "Tuple(", "Map(", "Nested(")
_BIG_INT_TYPES = frozenset(["Int128", "UInt128", "Int256", "UInt256"])


def _decode_string(value: bytes) -> str | bytes:
    # Like clickhouse_driver, fall back to bytes for strings which are not valid UTF-8
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value


def _split_type_args(args: str) -> list[str]:
    """Splits the arguments of a parametric type, e.g. `"sum, Array(UInt8)"`, on top-level commas."""
    parts, depth, start, quoted = [], 0, 0, False
    for i, char in enumerate(args):
        if char == "'" and (i == 0 or args[i - 1] != "\\"):
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(args[start:i].strip())
            start = i + 1
    parts.append(args[start:].strip())
    return parts


def _is_quoted_type(db_type: str) -> bool:
    """
    Whether TSV cells of `db_type` are written in the quoted text format, without escaping them for TSV: this is
    the case for composite types such as arrays, whose elements are quoted and escaped as in SQL.
    """
    if db_type.startswith("SimpleAggregateFunction("):
        return _is_quoted_type(_split_type_args(db_type[24:-1])[1])
    return db_type.startswith(_QUOTED_TYPE_PREFIXES)


def _cell_parser(db_type: str, timezone: datetime.tzinfo) -> Callable[[bytes], Any]:
    """
    Returns a function converting a TSV cell of `db_type` to the value assigned to a model field: its text, except
    for datetimes, which are converted according to the column type (naive for columns without a timezone, and
    aware in the column's timezone otherwise), like `select_rows` and `clickhouse_driver` do.
    """
    quoted = _is_quoted_type(db_type)
    if _has_datetime(db_type):
        convert = _row_converter(db_type, timezone)
        if quoted:
            return convert
        return lambda cell: None if cell == b"\\N" else convert(codecs.escape_decode(cell)[0])
    if quoted:
        return lambda cell: cell.decode("utf-8")
    return lambda cell: unescape(cell.decode("utf-8"))


def _has_datetime(db_type: str) -> bool:
    """Whether `db_type` is or contains a `DateTime` / `DateTime64` type (ignoring quoted strings, such as enum labels)."""
    return _DATETIME_TYPE.search(_QUOTED_STRING.sub("", db_type)) is not None


def _row_converter(db_type: str, timezone: datetime.tzinfo) -> Callable[[bytes], Any]:
    """
    Returns a function converting a non-NULL TSV value of `db_type` to its Python value. The value must be
    unescaped, unless it is written in the quoted text format (see `_is_quoted_type`).
    """
    for wrapper in ("Nullable(", "LowCardinality("):
        if db_type.startswith(wrapper):
            return _row_converter(db_type[len(wrapper) : -1], timezone)
    if db_type.startswith("SimpleAggregateFunction("):
        return _row_converter(_split_type_args(db_type[24:-1])[1], timezone)
    if db_type == "String" or db_type.startswith(_TEXT_TYPE_PREFIXES):
        return _decode_string
    if db_type.startswith("FixedString("):
        return lambda value: _decode_string(value.rstrip(b"\0"))
    if db_type == "Bool":
        return lambda value: value == b"true"
    if db_type in _BIG_INT_TYPES:
        return int
    if db_type == "Date32":
        return lambda value: datetime.date.fromisoformat(value.decode())
    if db_type == "Nothing":
        return lambda value: None
    try:
        field = ModelBase.create_ad_hoc_field(db_type)
    except (NotImplementedError, AssertionError):
        # Unsupported types (e.g. nested arrays) are rejected by the field classes with either exception
        return _decode_string
    if isinstance(field, ArrayField):
        # Convert the elements like values of the inner type (e.g. enums to names and datetimes to naive datetimes)
        inner_type = db_type[len("Array(") : -1]
        convert = _row_converter(inner_type, timezone)
        return lambda value: [None if item is None else convert(item.encode()) for item in parse_array(value.decode())]
    if isinstance(field, BaseEnumField):
        return lambda value: field.to_python(value.decode(), timezone).name
    # Datetimes are naive, unless the column has a timezone (which the ad-hoc field then has too)
    return lambda value: field.to_python(value.decode(), timezone)


__all__ = ["Codec", "RowResult", "TSVCodec"]
