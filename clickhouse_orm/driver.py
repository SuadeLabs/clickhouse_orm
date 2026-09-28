"""A driver is a specific implementation of communication with ClickHouse server."""

from __future__ import annotations

import abc
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol

import requests

from .codec import Codec, TSVCodec
from .exceptions import ServerError

#: The insert data sent by `RequestsDriver`: the request body, or an iterable of chunks streamed as the body.
InsertData = bytes | Iterable[bytes]


class DriverResponse(Protocol):
    """The interface of a response returned by `Driver.send`. `iter_lines` is only required by `TSVCodec`."""

    @property
    def text(self) -> str:
        """The full response body decoded as text."""
        ...

    def iter_lines(self) -> Iterator[bytes]:
        """Iterates over the lines of the response body."""
        ...


class Driver(abc.ABC):
    """
    Base class for ClickHouse drivers. Subclasses implement `send`; see "Custom Drivers" in the documentation.

    A driver is paired with the `codec` that understands its responses and produces its insert data. The default,
    `TSVCodec`, works with drivers whose responses provide `text` and `iter_lines()` (see `DriverResponse`) and which
    accept insert data as bytes.
    """

    #: The codec used by `Database` for the data sent to and received from this driver.
    codec: Codec = TSVCodec()

    @abc.abstractmethod
    def send(
        self,
        query: str,
        data: Any = None,
        settings: Mapping[str, str] | None = None,
        stream: bool = False,
        params: Mapping[str, str] | None = None,
    ) -> DriverResponse:
        """
        Sends a query to the ClickHouse server and returns the response.

        - `query`: the SQL statement to execute.
        - `data`: optional payload for the statement (e.g. rows for an INSERT statement), as produced by
          `self.codec.encode_inserts`. For `TSVCodec` it is an iterable of byte chunks, for streaming large inserts.
        - `settings`: query settings to send along with the query. `Database` also passes the name of its database
          here as `database` (once the database exists), which the driver must use for unqualified table names.
        - `stream`: if true, the response body is streamed rather than read eagerly (drivers may ignore this).
        - `params`: values for the query's `{name:Type}` placeholders, already encoded in ClickHouse's
          escaped text format (see `clickhouse_orm.params.format_param`).

        Raises `ServerError` if the server reports an error.
        """

    def scalar(
        self,
        query: str,
        data: Any = None,
        settings: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> str:
        """Sends a query to the ClickHouse server and returns the response text, stripped of whitespace."""
        return self.send(query, data=data, settings=settings, params=params).text.strip()


class RequestsDriver(Driver):
    """A ClickHouse driver that uses the HTTP interface via the requests library."""

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 60,
        verify_ssl_cert: bool = True,
    ):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl_cert
        if username:
            self.session.auth = (username, password or "")

    def send(
        self,
        query: str,
        data: InsertData | None = None,
        settings: Mapping[str, str] | None = None,
        stream: bool = False,
        params: Mapping[str, str] | None = None,
    ) -> requests.Response:
        url_params = dict(settings or {})
        for name, value in (params or {}).items():
            url_params["param_" + name] = value
        if data is None:
            body = query.encode("utf-8")
        else:
            # The HTTP interface joins the `query` parameter and the request body with a newline
            url_params["query"] = query.rstrip()
            body = data
        r = self.session.post(self.url, params=url_params, data=body, stream=stream, timeout=self.timeout)
        if r.status_code != 200:
            raise ServerError(r.text)
        return r


__all__ = ["Driver", "DriverResponse", "InsertData", "RequestsDriver"]
