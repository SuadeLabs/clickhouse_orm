"""A driver is a specific implementation of communication with ClickHouse server."""

from __future__ import annotations

import abc
from collections.abc import Iterable, Iterator, Mapping
from typing import Protocol

import requests

from .exceptions import ServerError

InsertData = bytes | Iterable[bytes]


class DriverResponse(Protocol):
    """The minimal interface of a response returned by `Driver.send`."""

    @property
    def text(self) -> str:
        """The full response body decoded as text."""
        ...

    def iter_lines(self) -> Iterator[bytes]:
        """Iterates over the lines of the response body."""
        ...


class Driver(abc.ABC):
    """Base class for ClickHouse drivers."""

    @abc.abstractmethod
    def send(
        self,
        query: str,
        data: InsertData | None = None,
        settings: Mapping[str, str] | None = None,
        stream: bool = False,
        params: Mapping[str, str] | None = None,
    ) -> DriverResponse:
        """
        Sends a query to the ClickHouse server and returns the response.

        - `query`: the SQL statement to execute.
        - `data`: optional payload for the statement (e.g. rows for an `INSERT ... FORMAT ...` query).
          May be an iterable of byte chunks for streaming large inserts.
        - `settings`: query settings to send along with the query.
        - `stream`: if true, the response body is streamed rather than read eagerly.
        - `params`: values for the query's `{name:Type}` placeholders, already encoded in ClickHouse's
          escaped text format (see `clickhouse_orm.params.format_param`).

        Raises `ServerError` if the server reports an error.
        """

    def scalar(
        self,
        query: str,
        data: InsertData | None = None,
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


__all__ = ["Driver", "DriverResponse", "RequestsDriver"]
