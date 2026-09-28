"""Exceptions raised by the ORM when communicating with ClickHouse."""

from __future__ import annotations

import re


class DatabaseException(Exception):  # noqa: N818
    """
    Raised when a database operation fails.
    """


class ServerError(DatabaseException):
    """
    Raised when a server returns an error.
    """

    def __init__(self, message):
        self.code = None
        processed = self.get_error_code_msg(message)
        if processed:
            self.code, self.message = processed
        else:
            # just skip custom init
            # if non-standard message format
            self.message = message
            super().__init__(message)

    ERROR_PATTERNS = (
        # ClickHouse prior to v19.3.3
        re.compile(
            r"""
            Code:\ (?P<code>\d+),
            \ e\.displayText\(\)\ =\ (?P<type1>[^ \n]+):\ (?P<msg>.+?),
            \ e.what\(\)\ =\ (?P<type2>[^ \n]+)
        """,
            re.VERBOSE | re.DOTALL,
        ),
        # ClickHouse v19.3.3+
        re.compile(
            r"""
            Code:\ (?P<code>\d+),
            \ e\.displayText\(\)\ =\ (?P<type1>[^ \n]+):\ (?P<msg>.+)
        """,
            re.VERBOSE | re.DOTALL,
        ),
        # ClickHouse v21+
        re.compile(
            r"""
            Code:\ (?P<code>\d+).
            \ (?P<type1>[^ \n]+):\ (?P<msg>.+)
        """,
            re.VERBOSE | re.DOTALL,
        ),
    )

    @classmethod
    def get_error_code_msg(cls, full_error_message):
        """
        Extract the code and message of the exception that clickhouse-server generated.

        See the list of error codes here:
        https://github.com/yandex/ClickHouse/blob/master/dbms/src/Common/ErrorCodes.cpp
        """
        for pattern in cls.ERROR_PATTERNS:
            match = pattern.match(full_error_message)
            if match:
                # assert match.group('type1') == match.group('type2')
                return int(match.group("code")), match.group("msg").strip()

        return 0, full_error_message

    def __str__(self):
        if self.code is not None:
            return f"{self.message} ({self.code})"


__all__ = ["DatabaseException", "ServerError"]
