from collections.abc import Iterator
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session

from app.config import settings

test_database_url = os.environ.get("TEST_DATABASE_URL")
if not test_database_url:
    raise RuntimeError("Set TEST_DATABASE_URL to a separate migrated PostgreSQL test database before running pytest")

application_url = make_url(settings.database_url)
test_url = make_url(test_database_url)
if test_url.get_backend_name() != "postgresql" or not test_url.database or not test_url.database.endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL must point to a PostgreSQL database ending in _test")
if application_url.database == test_url.database:
    raise RuntimeError("TEST_DATABASE_URL must not point to the application database")

os.environ["DATABASE_URL"] = test_database_url
settings.database_url = test_database_url

from app.db import engine, get_db
from app.main import app


@pytest.fixture
def database_client() -> Iterator[tuple[TestClient, Connection]]:
    with engine.connect() as connection:
        transaction = connection.begin()

        def test_db() -> Iterator[Session]:
            with Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client, connection
        finally:
            app.dependency_overrides.pop(get_db, None)
            transaction.rollback()