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
    ) -> DriverResponse:
        """
        Sends a query to the ClickHouse server and returns the response.

        - `query`: the SQL statement to execute.
        - `data`: optional payload for the statement (e.g. rows for an `INSERT ... FORMAT ...` query).
          May be an iterable of byte chunks for streaming large inserts.
        - `settings`: query settings / parameters to send along with the query.
        - `stream`: if true, the response body is streamed rather than read eagerly.

        Raises `ServerError` if the server reports an error.
        """

    def scalar(self, query: str, data: InsertData | None = None, settings: Mapping[str, str] | None = None) -> str:
        """Sends a query to the ClickHouse server and returns the response text, stripped of whitespace."""
        return self.send(query, data=data, settings=settings).text.strip()


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
    ) -> requests.Response:
        params = dict(settings or {})
        if data is None:
            body = query.encode("utf-8")
        else:
            # The HTTP interface joins the `query` parameter and the request body with a newline
            params["query"] = query.rstrip()
            body = data
        r = self.session.post(self.url, params=params, data=body, stream=stream, timeout=self.timeout)
        if r.status_code != 200:
            raise ServerError(r.text)
        return r


__all__ = ["Driver", "DriverResponse", "RequestsDriver"]
