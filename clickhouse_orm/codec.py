"""A codec converts model instances to and from the wire format used to exchange data with ClickHouse."""

from __future__ import annotations

import abc
import codecs
import datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytz

from .fields import BaseEnumField, DateTimeField
from .models import Model, ModelBase
from .utils import parse_tsv

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
    """Base class for codecs."""

    #: The ClickHouse format name used in the `FORMAT` clause of SELECT queries.
    select_format: str

    @abc.abstractmethod
    def insert_format(self, model_class: type[Model]) -> str:
        """Returns the ClickHouse format name used in the `FORMAT` clause when inserting `model_class` instances."""

    @abc.abstractmethod
    def encode(self, model_class: type[Model], instances: Iterable[Model], batch_size: int = 1000) -> Iterator[bytes]:
        """
        Serialises model instances into chunks of bytes in the model's insert format.

        - `model_class`: the model class of all the instances.
        - `instances`: the instances to serialise.
        - `batch_size`: the maximum number of instances per chunk.
        """

    @abc.abstractmethod
    def decode(
        self,
        response_lines: Iterable[bytes],
        model_class: type[Model] | None = None,
        timezone: datetime.tzinfo = pytz.utc,
    ) -> Iterator[Model]:
        """
        Deserialises the lines of a response in the select format into model instances.

        - `response_lines`: the lines of the response body.
        - `model_class`: the model class matching the query's columns,
          or `None` for getting back instances of an ad-hoc model.
        - `timezone`: the timezone for parsing dates and datetimes. Some fields use their own timezones.
        """

    @abc.abstractmethod
    def decode_rows(self, response_lines: Iterable[bytes], timezone: datetime.tzinfo = pytz.utc) -> RowResult:
        """
        Deserialises the lines of a response in the select format into a `RowResult`.

        Values use the same Python types as `clickhouse_driver`, so that results do not depend on the driver.
        In particular, `DateTime` columns without an explicit timezone are returned as naive datetimes in
        `timezone` (the server's timezone), and enums are returned as their names.

        - `response_lines`: the lines of the response body.
        - `timezone`: the timezone for parsing dates and datetimes. Some columns use their own timezones.
        """


class TSVCodec(Codec):
    """
    A codec using ClickHouse's tab-separated formats: `TabSeparatedWithNamesAndTypes` for reading,
    and `TabSeparated` (or `TSKV` for models with function expressions as defaults) for writing.
    """

    select_format = "TabSeparatedWithNamesAndTypes"

    def insert_format(self, model_class):
        # TSKV lets ClickHouse evaluate defaults for fields omitted from the row
        return "TSKV" if model_class.has_funcs_as_defaults() else "TabSeparated"

    def encode(self, model_class, instances, batch_size=1000):
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

    def decode(self, response_lines, model_class=None, timezone=pytz.utc):
        lines = iter(response_lines)
        field_names = parse_tsv(next(lines))
        field_types = parse_tsv(next(lines))
        model_class = model_class or ModelBase.create_ad_hoc_model(zip(field_names, field_types))
        for line in lines:
            # skip blank line left by WITH TOTALS modifier
            if line:
                yield model_class.from_tsv(line, field_names, timezone)

    def decode_rows(self, response_lines, timezone=pytz.utc):
        lines = iter(response_lines)
        columns = list(zip(parse_tsv(next(lines)), parse_tsv(next(lines))))
        converters = [_row_converter(db_type, timezone) for _, db_type in columns]
        return RowResult(columns, self._iter_rows(lines, converters))

    @staticmethod
    def _iter_rows(lines, converters):
        for line in lines:
            # skip blank line left by WITH TOTALS modifier
            if line:
                yield tuple(
                    None if value == b"\\N" else convert(codecs.escape_decode(value)[0])
                    for convert, value in zip(converters, line.split(b"\t"))
                )


# Types the ORM cannot parse yet are returned as their ClickHouse text representation
_TEXT_TYPE_PREFIXES = ("Tuple(", "Map(", "Nested(", "Variant(", "Dynamic", "JSON", "Object(", "AggregateFunction(")
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


def _row_converter(db_type: str, timezone: datetime.tzinfo) -> Callable[[bytes], Any]:
    """Returns a function converting an unescaped, non-NULL TSV value of `db_type` to its Python value."""
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
    if isinstance(field, BaseEnumField):
        return lambda value: field.to_python(value.decode(), timezone).name
    if isinstance(field, DateTimeField):
        if field.timezone:
            return lambda value: field.to_python(value.decode(), field.timezone)
        return lambda value: field.to_python(value.decode(), None)
    return lambda value: field.to_python(value.decode(), timezone)


__all__ = ["Codec", "RowResult", "TSVCodec"]
