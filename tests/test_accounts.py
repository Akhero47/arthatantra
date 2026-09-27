import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from models import Account


@pytest.mark.parametrize("body", [None, {}])
def test_create_account_starts_at_zero(
    database_client: tuple[TestClient, Connection], body: dict[str, object] | None
) -> None:
    client, _ = database_client
    response = client.post("/accounts", json=body)

    assert response.status_code == 201
    account = response.json()
    assert isinstance(account["id"], int)
    assert account["balance"] == 0
    assert account["created_at"]
    assert account["updated_at"]
    assert client.get(f"/accounts/{account['id']}").json() == account


def test_account_ids_are_distinct(database_client: tuple[TestClient, Connection]) -> None:
    client, _ = database_client
    first = client.post("/accounts").json()
    second = client.post("/accounts").json()

    assert first["id"] != second["id"]


def test_get_account_returns_current_balance(database_client: tuple[TestClient, Connection]) -> None:
    client, connection = database_client
    account_id = client.post("/accounts").json()["id"]

    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        account = session.get(Account, account_id)
        assert account is not None
        account.balance = 125
        session.commit()

    response = client.get(f"/accounts/{account_id}")
    assert response.status_code == 200
    assert response.json()["balance"] == 125


def test_missing_account_returns_404(database_client: tuple[TestClient, Connection]) -> None:
    client, _ = database_client
    response = client.get("/accounts/9223372036854775807")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "ACCOUNT_NOT_FOUND", "message": "Account not found"}}


@pytest.mark.parametrize("account_id", ["0", "-1", "not-an-id", "9223372036854775808"])
def test_invalid_account_id_returns_422(
    database_client: tuple[TestClient, Connection], account_id: str
) -> None:
    client, _ = database_client
    assert client.get(f"/accounts/{account_id}").status_code == 422


@pytest.mark.parametrize("body", [{"balance": 100}, {"unknown": 1}, []])
def test_create_account_rejects_input(
    database_client: tuple[TestClient, Connection], body: object
) -> None:
    client, _ = database_client
    assert client.post("/accounts", json=body).status_code == 422