from __future__ import annotations

import contextlib
import os
import pathlib
import time
from unittest import mock

import docker
import docker.errors
import pytest

from clickhouse_orm.database import Database

_HERE = pathlib.Path(__file__).parent.resolve()

#: The address of the server's native protocol, for tests of `NativeDriver`
NATIVE_URL_ENV = "CLICKHOUSE_NATIVE_URL"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--driver",
        choices=["http", "native"],
        default="http",
        help="the driver used by Database instances created without one (native requires clickhouse-driver, and "
        "connects to $%s, by default clickhouse://localhost:9000)" % NATIVE_URL_ENV,
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "http_only: tests which only apply to the default HTTP driver")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--driver") == "native":
        skip = pytest.mark.skip(reason="only applies to the HTTP driver")
        for item in items:
            if "http_only" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def default_driver(request: pytest.FixtureRequest, clickhouse_db: Database):
    """With `--driver=native`, databases created without a driver use `NativeDriver` instead of `RequestsDriver`."""
    if request.config.getoption("--driver") != "native":
        yield
        return
    from clickhouse_orm.native import NativeDriver

    url = os.environ.get(NATIVE_URL_ENV, "clickhouse://localhost:9000")

    def create_driver(db_url, username=None, password=None, **kwargs):
        driver = NativeDriver.from_url(url)
        if username:
            driver.client_kwargs.update(user=username, password=password or "")
        return driver

    with mock.patch("clickhouse_orm.database.RequestsDriver", side_effect=create_driver):
        yield


# The following allows running tests locally with a temporary Clickhouse docker container
@pytest.fixture(scope="session", autouse=True)
def clickhouse_db(request: pytest.FixtureRequest) -> Database:
    """Factory function to create a temporary Clickhouse database for testing."""
    try:
        return setup_github_db(request)
    except Exception:
        return setup_local_db(request)


def setup_github_db(request: pytest.FixtureRequest) -> Database:
    """Set up a connection to an existing database - e.g. on GitHub Actions."""
    return Database("test-db", log_statements=True)


def setup_local_db(request: pytest.FixtureRequest) -> Database:
    """Get config for db we spin up ourselves - e.g. locally."""
    docker_client = docker.APIClient()
    container_name = "pytest_clickhouse"
    orig_url = Database._default_url

    def rm_local_db():
        Database._default_url = orig_url
        with contextlib.suppress(docker.errors.NotFound):
            docker_client.kill(container_name)
        with contextlib.suppress(docker.errors.NotFound):
            docker_client.remove_container(container_name)

    request.addfinalizer(rm_local_db)
    database = start_db_container(docker_client, container_name)
    Database._default_url = database.db_url
    return database


def start_db_container(client: docker.APIClient, container_name: str) -> Database:
    """Start a docker database container running Clickhouse."""
    print("Starting Clickhouse container...")
    client.pull("clickhouse/clickhouse-server:25.8")
    container = client.create_container(
        "clickhouse/clickhouse-server:25.8",
        name=container_name,
        detach=True,
        ports={"8123/tcp": {}, "9000/tcp": {}},
        stdin_open=False,
        tty=False,
        host_config=client.create_host_config(
            binds={
                str(_HERE / "remote-servers.xml"): {
                    "bind": "/etc/clickhouse-server/config.d/remote_servers.xml",
                    "mode": "ro",
                }
            },
            tmpfs={"/var/lib/clickhouse": "size=4G,uid=999"},
            volumes_from=[],
            publish_all_ports=True,
        ),
        environment={"CLICKHOUSE_SKIP_USER_SETUP": "1"},
    )
    client.start(container=container_name)
    container_id = container["Id"]
    container_port = int(client.port(container_id, 8123)[0]["HostPort"])
    native_port = int(client.port(container_id, 9000)[0]["HostPort"])
    os.environ.setdefault(NATIVE_URL_ENV, f"clickhouse://localhost:{native_port}")

    # give the container time to spin up
    print("Waiting for Clickhouse to start...")
    attemtps = 10
    for k in range(1, attemtps + 1):
        time.sleep(1)
        try:
            return Database("test-db", db_url=f"http://localhost:{container_port}", log_statements=True)
        except Exception as e:
            print(f"Attempt {k}: {e}")
            if k == attemtps:
                raise
