"""SQL generation helpers that do not require a connection to a ClickHouse server."""

from __future__ import annotations

import dataclasses
import warnings
from string import Template
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Model


@dataclasses.dataclass(frozen=True)
class ServerCapabilities:
    """
    Optional ClickHouse server features that affect the generated DDL.

    The defaults describe a modern server. Use `ServerCapabilities.from_version()` to
    derive the flags from a server version, or override individual flags as needed.
    """

    # Custom partitioning keys (PARTITION BY / ORDER BY) were introduced in 1.1.54310
    has_custom_partitioning: bool = True
    # Column compression codecs were introduced in 19.1.16
    has_codec_support: bool = True
    # LowCardinality columns were introduced in 19.0
    has_low_cardinality_support: bool = True

    @classmethod
    def from_version(cls, server_version: tuple[int, ...]) -> ServerCapabilities:
        """Returns the capabilities of a ClickHouse server with the given version tuple."""
        return cls(
            has_custom_partitioning=server_version >= (1, 1, 54310),
            has_codec_support=server_version >= (19, 1, 16),
            has_low_cardinality_support=server_version >= (19, 0),
        )


def quote_identifier(name: str) -> str:
    """Wraps an identifier (database, table, column name) in backticks."""
    return "`%s`" % name


def qualified_name(db_name: str, table_name: str) -> str:
    """Returns the backtick-quoted, database-qualified name of a table."""
    return "%s.%s" % (quote_identifier(db_name), quote_identifier(table_name))


def model_table_ref(db_name: str, model_class: type[Model]) -> str:
    """Returns the qualified table name of a model, taking system models into account."""
    return qualified_name("system" if model_class.is_system_model() else db_name, model_class.table_name())


def substitute(query: str, db_name: str, model_class: type[Model] | None = None) -> str:
    """
    Replaces the `$db` and (if `model_class` is given) `$table` placeholders in a query
    with fully-qualified, backtick-quoted names.
    """
    if "$" not in query:
        return query
    mapping = {"db": quote_identifier(db_name)}
    if model_class:
        mapping["table"] = model_table_ref(db_name, model_class)
    return Template(query).safe_substitute(mapping)


def resolve_ddl_target(db_name: Any, capabilities: ServerCapabilities | None, caller: str):
    """
    Normalises the arguments of the DDL generation methods into a `(db_name, capabilities)` pair.

    Passing a `Database` instance instead of a database name is supported for backwards
    compatibility, but is deprecated.
    """
    if isinstance(db_name, str):
        return db_name, capabilities or ServerCapabilities()
    warnings.warn(
        f"Passing a Database to {caller}() is deprecated, pass the database name and a ServerCapabilities instead",
        DeprecationWarning,
        stacklevel=3,
    )
    return db_name.db_name, capabilities or db_name.capabilities


__all__ = ["ServerCapabilities"]
