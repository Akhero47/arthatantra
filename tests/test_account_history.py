from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.models import Account, AccountTransaction


@pytest.fixture
def account_ids(database_client: tuple[TestClient, Connection]) -> tuple[int, int]:
    _, connection = database_client
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        source = Account(balance=100)
        destination = Account()
        session.add_all((source, destination))
        session.commit()
        return source.id, destination.id


def make_transfer(client: TestClient, source_id: int, destination_id: int, amount: int) -> int:
    response = client.post("/transfers", json={
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": amount
    }, headers={"Idempotency-Key": f"transfer-{source_id}-{destination_id}-{amount}"})
    assert response.status_code == 201
    return response.json()["id"]


@pytest.fixture
def mixed_history(database_client: tuple[TestClient, Connection], account_ids: tuple[int, int]) -> list[int]:
    client, _ = database_client
    source_id, destination_id = account_ids
    return [
        make_transfer(client, source_id, destination_id, 30),
        make_transfer(client, destination_id, source_id, 10),
        make_transfer(client, source_id, destination_id, 15),
    ]


def test_empty_history(database_client: tuple[TestClient, Connection], account_ids: tuple[int, int]) -> None:
    client, _ = database_client
    source_id, _ = account_ids
    response = client.get(f"/accounts/{source_id}/transactions")

    assert response.status_code == 200
    assert response.json() == {"items": [], "page": 1, "limit": 20, "total": 0}


def test_debit_history(database_client: tuple[TestClient, Connection], account_ids: tuple[int, int]) -> None:
    client, _ = database_client
    source_id, destination_id = account_ids
    transfer_id = make_transfer(client, source_id, destination_id, 30)
    response = client.get(f"/accounts/{source_id}/transactions")

    assert response.status_code == 200
    entry = response.json()["items"][0]
    assert isinstance(entry["id"], int)
    assert (entry["account_id"], entry["transfer_id"], entry["direction"], entry["amount"]) == (
        source_id, transfer_id, "debit", 30
    )
    assert entry["created_at"]
    assert (response.json()["page"], response.json()["limit"], response.json()["total"]) == (1, 20, 1)


def test_credit_history(database_client: tuple[TestClient, Connection], account_ids: tuple[int, int]) -> None:
    client, _ = database_client
    source_id, destination_id = account_ids
    transfer_id = make_transfer(client, source_id, destination_id, 30)
    response = client.get(f"/accounts/{destination_id}/transactions")

    assert response.status_code == 200
    entry = response.json()["items"][0]
    assert (entry["account_id"], entry["transfer_id"], entry["direction"], entry["amount"]) == (
        destination_id, transfer_id, "credit", 30
    )


def test_multiple_transactions_newest_first(
    database_client: tuple[TestClient, Connection], account_ids: tuple[int, int], mixed_history: list[int]
) -> None:
    client, connection = database_client
    source_id, _ = account_ids
    first, second, third = mixed_history
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        first_entry = session.scalar(select(AccountTransaction).where(
            AccountTransaction.account_id == source_id, AccountTransaction.transfer_id == first
        ))
        tied_entries = session.scalars(select(AccountTransaction).where(
            AccountTransaction.account_id == source_id, AccountTransaction.transfer_id.in_((second, third))
        )).all()
        assert first_entry is not None
        assert len(tied_entries) == 2
        assert tied_entries[0].created_at == tied_entries[1].created_at
        first_entry.created_at = datetime.now(timezone.utc) + timedelta(days=1)
        session.commit()

    response = client.get(f"/accounts/{source_id}/transactions")
    assert response.status_code == 200
    entries = response.json()["items"]
    assert [(entry["transfer_id"], entry["direction"], entry["amount"]) for entry in entries] == [
        (first, "debit", 30), (third, "debit", 15), (second, "credit", 10)
    ]
    assert response.json()["total"] == 3


def test_page_pagination(
    database_client: tuple[TestClient, Connection], account_ids: tuple[int, int], mixed_history: list[int]
) -> None:
    client, _ = database_client
    source_id, _ = account_ids
    first_page = client.get(f"/accounts/{source_id}/transactions?page=1&limit=2")
    second_page = client.get(f"/accounts/{source_id}/transactions?page=2&limit=2")
    empty_page = client.get(f"/accounts/{source_id}/transactions?page=3&limit=2")

    assert first_page.status_code == second_page.status_code == empty_page.status_code == 200
    assert [entry["transfer_id"] for entry in first_page.json()["items"]] == mixed_history[::-1][:2]
    assert [entry["transfer_id"] for entry in second_page.json()["items"]] == mixed_history[::-1][2:]
    assert (first_page.json()["page"], first_page.json()["limit"], first_page.json()["total"]) == (1, 2, 3)
    assert (second_page.json()["page"], second_page.json()["limit"], second_page.json()["total"]) == (2, 2, 3)
    assert empty_page.json() == {"items": [], "page": 3, "limit": 2, "total": 3}


def test_maximum_limit(
    database_client: tuple[TestClient, Connection], account_ids: tuple[int, int], mixed_history: list[int]
) -> None:
    client, _ = database_client
    source_id, _ = account_ids
    response = client.get(f"/accounts/{source_id}/transactions?limit=100")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3
    assert (response.json()["page"], response.json()["limit"], response.json()["total"]) == (1, 100, 3)


def test_history_does_not_query_per_entry(
    database_client: tuple[TestClient, Connection], account_ids: tuple[int, int], mixed_history: list[int]
) -> None:
    client, connection = database_client
    source_id, _ = account_ids
    selects = []

    def record_query(connection: Connection, cursor: object, statement: str, parameters: object,
                     context: object, many: bool) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(connection, "before_cursor_execute", record_query)
    try:
        response = client.get(f"/accounts/{source_id}/transactions")
    finally:
        event.remove(connection, "before_cursor_execute", record_query)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 3
    assert len(selects) == 3


def test_nonexistent_account_returns_404(database_client: tuple[TestClient, Connection]) -> None:
    client, _ = database_client
    response = client.get("/accounts/9223372036854775807/transactions")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "ACCOUNT_NOT_FOUND", "message": "Account not found"}}


@pytest.mark.parametrize("query", ["page=0", "page=-1", "limit=0", "limit=101"])
def test_invalid_pagination_returns_422(
    database_client: tuple[TestClient, Connection], account_ids: tuple[int, int], query: str
) -> None:
    client, _ = database_client
    source_id, _ = account_ids
    response = client.get(f"/accounts/{source_id}/transactions?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["location"][0] == "query"