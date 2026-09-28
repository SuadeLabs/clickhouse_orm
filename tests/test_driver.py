from __future__ import annotations

import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from clickhouse_orm.database import Database, ServerError
from clickhouse_orm.driver import Driver, RequestsDriver

from .base_test_with_data import Person, TestCaseWithData


def _response(text="", status_code=200):
    return mock.Mock(text=text, status_code=status_code)


class RequestsDriverTestCase(unittest.TestCase):
    def setUp(self):
        self.driver = RequestsDriver("http://example.com:8123/", timeout=5)
        patcher = mock.patch.object(self.driver.session, "post", return_value=_response("1\n"))
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_session_configuration(self):
        driver = RequestsDriver("http://example.com:8123/", username="user", verify_ssl_cert=False)
        self.assertEqual(driver.session.auth, ("user", ""))
        self.assertFalse(driver.session.verify)
        self.assertIsNone(RequestsDriver("http://example.com:8123/").session.auth)

    def test_send_query_in_body(self):
        self.driver.send("SELECT 1", settings={"database": "db"}, stream=True)
        self.post.assert_called_once_with(
            "http://example.com:8123/", params={"database": "db"}, data=b"SELECT 1", stream=True, timeout=5
        )

    def test_send_with_data(self):
        payload = [b"row1\n", b"row2\n"]
        self.driver.send("INSERT INTO t FORMAT TabSeparated\n", data=payload, settings={"database": "db"})
        self.post.assert_called_once_with(
            "http://example.com:8123/",
            params={"database": "db", "query": "INSERT INTO t FORMAT TabSeparated"},
            data=payload,
            stream=False,
            timeout=5,
        )

    def test_send_does_not_mutate_settings(self):
        settings = {"database": "db"}
        self.driver.send("INSERT INTO t FORMAT TabSeparated\n", data=b"row\n", settings=settings)
        self.assertEqual(settings, {"database": "db"})

    def test_scalar(self):
        self.assertEqual(self.driver.scalar("SELECT 1"), "1")

    def test_server_error(self):
        self.post.return_value = _response("Code: 62. DB::Exception: Syntax error", status_code=400)
        with self.assertRaises(ServerError) as cm:
            self.driver.send("SELEC 1")
        self.assertEqual(cm.exception.code, 62)

    def test_driver_is_abstract(self):
        with self.assertRaises(TypeError):
            Driver()


@pytest.mark.http_only
class DatabaseDriverTestCase(TestCaseWithData):
    def test_default_driver(self):
        self.assertIsInstance(self.database.driver, RequestsDriver)
        self.assertIs(self.database.request_session, self.database.driver.session)

    def test_all_io_goes_through_driver(self):
        with mock.patch.object(self.database.driver, "send", wraps=self.database.driver.send) as send:
            self._insert_and_check(self._sample_data(), 100)
            self.database.does_table_exist(Person)
            list(self.database.select("SELECT * FROM $table LIMIT 1", Person))
        queries = [c.args[0] for c in send.call_args_list]
        self.assertTrue(any(q.startswith("INSERT INTO") for q in queries))
        self.assertTrue(any("system.tables" in q for q in queries))
        self.assertTrue(any("FORMAT TabSeparatedWithNamesAndTypes" in q for q in queries))
        for c in send.call_args_list:
            self.assertEqual(c.kwargs["settings"]["database"], self.database.db_name)

    def test_readonly_setting_passed_to_driver(self):
        db = Database(self.database.db_name, readonly=True)
        if db.connection_readonly:
            self.skipTest("Connection is already readonly")
        with mock.patch.object(db.driver, "send", wraps=db.driver.send) as send:
            db.count(Person)
        self.assertEqual(send.call_args.kwargs["settings"]["readonly"], "1")


class UrllibResponse:
    def __init__(self, text):
        self.text = text

    def iter_lines(self):
        return (line.encode() for line in self.text.splitlines())


class UrllibDriver(Driver):
    """A minimal driver, implemented outside the package, which uses the standard library to talk to the HTTP API."""

    def __init__(self, url):
        self.url = url
        self.queries = []

    def send(self, query, data=None, settings=None, stream=False, params=None):
        self.queries.append(query)
        url_params = dict(settings or {})
        url_params.update(("param_" + name, value) for name, value in (params or {}).items())
        if data is None:
            body = query.encode()
        else:
            url_params["query"] = query
            body = b"".join(data)
        try:
            with urlopen(self.url.rstrip("/") + "/?" + urlencode(url_params), data=body) as response:
                return UrllibResponse(response.read().decode())
        except HTTPError as e:
            raise ServerError(e.read().decode())


class CustomDriverTestCase(TestCaseWithData):
    def setUp(self):
        super().setUp()
        self.driver = UrllibDriver(Database._default_url)
        self.database = Database(self.database.db_name, driver=self.driver)

    def test_database_uses_driver(self):
        self.assertIs(self.database.driver, self.driver)
        self._insert_and_check(self._sample_data(), 100)
        qs = Person.objects_in(self.database).filter(first_name="Whitney").order_by("last_name")
        self.assertEqual([p.last_name for p in qs.parameterized()], ["Durham", "Scott"])
        self.assertEqual(list(self.database.select_rows("SELECT 1 AS x")), [(1,)])
        self.assertTrue(any(q.startswith("INSERT INTO") for q in self.driver.queries))

    def test_server_error(self):
        with self.assertRaises(ServerError) as cm:
            self.database.raw("SELECT * FROM no_such_table")
        self.assertEqual(cm.exception.code, 60)
