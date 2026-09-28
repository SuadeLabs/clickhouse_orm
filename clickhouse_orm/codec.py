"""A codec converts model instances to and from the wire format used to exchange data with ClickHouse."""

from __future__ import annotations

import abc
from io import BytesIO
from typing import TYPE_CHECKING

import pytz

from .models import Model, ModelBase
from .utils import parse_tsv

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable, Iterator


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


__all__ = ["Codec", "TSVCodec"]
