from __future__ import annotations

import codecs
import importlib
import pkgutil
import re
from datetime import date, datetime, timedelta, tzinfo
from inspect import isclass
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType
    from typing import Any


class Page(NamedTuple):
    """A simple data structure for paginated results."""

    objects: list[Any]
    number_of_objects: int
    pages_total: int
    number: int
    page_size: int


def escape(value: str, quote: bool = True) -> str:
    """
    If the value is a string, escapes any special characters and optionally
    surrounds it with single quotes. If the value is not a string (e.g. a number),
    converts it to one.
    """
    value = codecs.escape_encode(value.encode("utf-8"))[0].decode("utf-8")
    if quote:
        value = "'" + value + "'"

    return value


def unescape(value: str) -> str | None:
    if value == "\\N":
        return None
    return codecs.escape_decode(value)[0].decode("utf-8")


def string_or_func(obj):
    return obj.to_sql() if hasattr(obj, "to_sql") else obj


def arg_to_sql(arg: Any) -> str:
    """
    Converts a function argument to SQL string according to its type.
    Supports functions, model fields, strings, dates, datetimes, timedeltas, booleans,
    None, numbers, timezones, arrays/iterables.
    """
    from clickhouse_orm import DateTimeField, F, Field, QuerySet, StringField
    from clickhouse_orm.params import active_params

    if isinstance(arg, F):
        return arg.to_sql()
    if isinstance(arg, Field):
        return "`%s`" % arg
    # Inside a `collect_params()` block, strings, dates and datetimes are bound as query parameters
    params = active_params()
    if isinstance(arg, str):
        return params.bind(escape(arg, quote=False), "String") if params else StringField().to_db_string(arg)
    if isinstance(arg, datetime):
        timestamp = DateTimeField().to_db_string(arg, quote=False)
        return params.bind(timestamp, "DateTime") if params else "toDateTime('%s')" % timestamp
    if isinstance(arg, date):
        return params.bind(arg.isoformat(), "Date") if params else "toDate('%s')" % arg.isoformat()
    if isinstance(arg, timedelta):
        return "toIntervalSecond(%d)" % int(arg.total_seconds())
    if isinstance(arg, bool):
        return str(int(arg))
    if isinstance(arg, tzinfo):
        return StringField().to_db_string(arg.tzname(None))
    if arg is None:
        return "NULL"
    if isinstance(arg, QuerySet):
        return "(%s)" % arg
    if isinstance(arg, tuple):
        return "(" + comma_join(arg_to_sql(x) for x in arg) + ")"
    if is_iterable(arg):
        return "[" + comma_join(arg_to_sql(x) for x in arg) + "]"
    return str(arg)


def split_tsv(line: bytes | str) -> list[str]:
    """Splits a TSV line into its cells, without unescaping them."""
    if isinstance(line, bytes):
        line = line.decode()
    if line and line[-1] == "\n":
        line = line[:-1]
    return line.split("\t")


def parse_tsv(line: bytes | str) -> list[str]:
    """Splits a TSV line into its unescaped cells."""
    return [unescape(value) for value in split_tsv(line)]


_QUOTED_ITEM = re.compile(r"'((?:[^'\\]|\\.)*)'", re.DOTALL)
_UNQUOTED_ITEM = re.compile(r"[^,'\[\](){}]*")
_CLOSING_BRACKETS = {"[": "]", "(": ")", "{": "}"}


def parse_array(array_string: str) -> list[Any]:
    """
    Parse an array or tuple string as returned by clickhouse. For example:
        "['hello', 'world']" ==> ["hello", "world"]
        "(1,2,3)"            ==> ["1", "2", "3"]
        "[1,NULL]"           ==> ["1", None]
        "[(1,'a'),[2]]"      ==> ["(1,'a')", "[2]"]

    Quoted values are unescaped, and nested arrays, tuples and maps are returned as their text, to be parsed by the
    field of the element. The string must be in ClickHouse's quoted text format, as in SQL and in the TSV cells of
    arrays and tuples (which, unlike other cells, are not escaped for TSV).
    """
    if len(array_string) < 2 or _CLOSING_BRACKETS.get(array_string[0]) != array_string[-1] or array_string[0] == "{":
        raise ValueError('Invalid array string: "%s"' % array_string)
    values: list[Any] = []
    pos, end = 1, len(array_string) - 1
    if not array_string[pos:end].strip():
        return values
    while True:
        pos = _skip_spaces(array_string, pos, end)
        char = array_string[pos] if pos < end else ""
        if char == "'":
            match = _QUOTED_ITEM.match(array_string, pos, end)
            if match is None:
                raise ValueError('Invalid array string: "%s"' % array_string)
            values.append(codecs.escape_decode(match.group(1).encode("utf-8"))[0].decode("utf-8"))
            pos = match.end()
        elif char in _CLOSING_BRACKETS:
            item_end = _composite_end(array_string, pos, end)
            values.append(array_string[pos:item_end])
            pos = item_end
        else:
            match = _UNQUOTED_ITEM.match(array_string, pos, end)
            item = match.group().strip()
            if not item:
                raise ValueError('Invalid array string: "%s"' % array_string)
            values.append(None if item == "NULL" else item)
            pos = match.end()
        pos = _skip_spaces(array_string, pos, end)
        if pos == end:
            return values
        if array_string[pos] != ",":
            raise ValueError('Invalid array string: "%s"' % array_string)
        pos += 1
        # A trailing comma is allowed in single-element tuples, e.g. "(1,)"
        if array_string[0] == "(" and len(values) == 1 and _skip_spaces(array_string, pos, end) == end:
            return values


def _skip_spaces(text: str, pos: int, end: int) -> int:
    while pos < end and text[pos].isspace():
        pos += 1
    return pos


def _composite_end(text: str, start: int, end: int) -> int:
    """Returns the position after the array, tuple or map starting at `start`, skipping over quoted strings."""
    stack = []
    pos = start
    while pos < end:
        char = text[pos]
        if char == "'":
            match = _QUOTED_ITEM.match(text, pos, end)
            if match is None:
                break
            pos = match.end()
            continue
        if char in _CLOSING_BRACKETS:
            stack.append(_CLOSING_BRACKETS[char])
        elif char in ")]}":
            if char != stack.pop():
                break
            if not stack:
                return pos + 1
        pos += 1
    raise ValueError('Invalid array string: "%s"' % text)


def split_type_args(args: str) -> list[str]:
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
    return parts if parts != [""] else []


_TUPLE_ELEMENT_NAME = re.compile(r"(`(?:[^`\\]|\\.)*`|[A-Za-z_][A-Za-z0-9_]*)\s+(\S.*)", re.DOTALL)


def parse_tuple_type(db_type: str) -> list[tuple[str | None, str]]:
    """
    Returns the `(name, type)` pairs of the elements of a `Tuple` type, with `None` names for unnamed elements.
    For example `"Tuple(a UInt8, b Nullable(String))"` ==> `[("a", "UInt8"), ("b", "Nullable(String)")]`.
    """
    elements = []
    for element in split_type_args(db_type[len("Tuple(") : -1]):
        # Unnamed element types never start with an identifier followed by a space (e.g. "Decimal(9, 2)")
        match = _TUPLE_ELEMENT_NAME.fullmatch(element)
        if match:
            name = match.group(1)
            if name.startswith("`"):
                name = codecs.escape_decode(name[1:-1].encode("utf-8"))[0].decode("utf-8")
            elements.append((name, match.group(2)))
        else:
            elements.append((None, element))
    return elements


def import_submodules(package_name: str) -> dict[str, ModuleType]:
    """
    Import all submodules of a module.
    """
    package = importlib.import_module(package_name)
    return {
        name: importlib.import_module(package_name + "." + name)
        for _, name, _ in pkgutil.iter_modules(package.__path__)
    }


def comma_join(items: Iterable[str]) -> str:
    """
    Joins an iterable of strings with commas.
    """
    return ", ".join(items)


def is_iterable(obj: Any) -> bool:
    """
    Checks if the given object is iterable.
    """
    try:
        iter(obj)
        return True
    except TypeError:
        return False


def get_subclass_names(locals: dict[str, Any], base_class: type):
    return [c.__name__ for c in locals.values() if isclass(c) and issubclass(c, base_class)]


class NoValue:
    """
    A sentinel for fields with an expression for a default value,
    that were not assigned a value yet.
    """

    def __repr__(self):
        return "NO_VALUE"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


NO_VALUE = NoValue()
