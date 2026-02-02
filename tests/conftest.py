from __future__ import annotations

import time

import docker
import pytest

from clickhouse_orm.database import Database


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
        try:
            docker_client.kill(container_name)
        finally:
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
        ports={"8123/tcp": {}},
        stdin_open=False,
        tty=False,
        host_config=client.create_host_config(
            binds=[],
            tmpfs={"/var/lib/clickhouse": "size=4G,uid=999"},
            volumes_from=[],
            publish_all_ports=True,
        ),
        environment={"CLICKHOUSE_SKIP_USER_SETUP": "1"},
    )
    client.start(container=container_name)
    container_id = container["Id"]
    container_port = int(client.port(container_id, 8123)[0]["HostPort"])

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
