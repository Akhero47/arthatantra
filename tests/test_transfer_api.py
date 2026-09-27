from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, event, func, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import transfer_repository
from db import SessionLocal, engine
from main import app
from models import Account, AccountTransaction, Transfer
from transfer_request_hash import hash_transfer_request


def post_transfer(client: TestClient, body: dict, key: str | None = None) -> Response:
    return client.post("/transfers", json=body, headers={"Idempotency-Key": key or uuid4().hex})


@pytest.fixture
def funded_accounts(database_client: tuple[TestClient, Connection]) -> tuple[int, int]:
    _, connection = database_client
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        source = Account(balance=100)
        destination = Account()
        session.add_all((source, destination))
        session.commit()
        return source.id, destination.id


def test_successful_transfer(database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int]) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    response = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": 40
    }, key="first-transfer")

    assert response.status_code == 201
    transfer = response.json()
    assert isinstance(transfer["id"], int)
    assert transfer["source_account_id"] == source_id
    assert transfer["destination_account_id"] == destination_id
    assert transfer["amount"] == 40
    assert transfer["created_at"]
    assert client.get(f"/accounts/{source_id}").json()["balance"] == 60
    assert client.get(f"/accounts/{destination_id}").json()["balance"] == 40
    with Session(bind=connection) as session:
        stored = session.get(Transfer, transfer["id"])
        assert stored.idempotency_key == "first-transfer"
        assert stored.request_hash == hash_transfer_request(source_id, destination_id, 40)
        entries = session.scalars(select(AccountTransaction).where(AccountTransaction.transfer_id == transfer["id"])).all()
        assert {(entry.account_id, entry.direction, entry.amount) for entry in entries} == {
            (source_id, "debit", 40), (destination_id, "credit", 40)
        }


def test_retry_returns_existing_transfer_without_another_debit(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int]
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": 100}
    first = post_transfer(client, body, key="retry-key")
    replay = post_transfer(client, body, key="retry-key")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert client.get(f"/accounts/{source_id}").json()["balance"] == 0
    assert client.get(f"/accounts/{destination_id}").json()["balance"] == 100
    with Session(bind=connection) as session:
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 1
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 2


def test_same_key_different_request_is_rejected(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int]
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    first = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": 40
    }, key="conflicting-key")
    conflict = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": 41
    }, key="conflicting-key")

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json() == {"error": {
        "code": "IDEMPOTENCY_KEY_REUSED", "message": "Idempotency key already used for a different transfer"
    }}
    assert client.get(f"/accounts/{source_id}").json()["balance"] == 60
    assert client.get(f"/accounts/{destination_id}").json()["balance"] == 40
    with Session(bind=connection) as session:
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 1
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 2


@pytest.mark.parametrize("changed_field", ["source_account_id", "destination_account_id"])
def test_key_conflict_precedes_same_account_business_rule(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], changed_field: str
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": 10}
    first = post_transfer(client, body, key="different-account-key")
    changed = dict(body)
    changed[changed_field] = destination_id if changed_field == "source_account_id" else source_id
    conflict = post_transfer(client, changed, key="different-account-key")

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json() == {"error": {
        "code": "IDEMPOTENCY_KEY_REUSED", "message": "Idempotency key already used for a different transfer"
    }}
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 90
        assert session.get(Account, destination_id).balance == 10
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 1
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 2


@pytest.mark.parametrize("key", [None, "bad key", "x" * 256])
def test_idempotency_key_is_required_and_validated(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], key: str | None
) -> None:
    client, _ = database_client
    source_id, destination_id = funded_accounts
    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": 10}
    headers = {"Idempotency-Key": key} if key is not None else {}
    response = client.post("/transfers", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/accounts/{source_id}").json()["balance"] == 100


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "10", 2**63])
def test_invalid_amount_returns_422(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], amount: object
) -> None:
    client, _ = database_client
    source_id, destination_id = funded_accounts
    response = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": amount
    })
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_AMOUNT"
    assert error["message"] == "Invalid transfer amount"
    assert all(set(issue) == {"location", "message", "type"} for issue in error["details"])
    assert client.get(f"/accounts/{source_id}").json()["balance"] == 100


@pytest.mark.parametrize("field", ["source_account_id", "destination_account_id"])
@pytest.mark.parametrize("value", [0, -1, True, "1", 2**63])
def test_invalid_account_id_returns_422(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], field: str, value: object
) -> None:
    client, _ = database_client
    source_id, destination_id = funded_accounts
    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": 10}
    body[field] = value
    response = post_transfer(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_insufficient_balance_returns_409(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int]
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    response = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": 101
    })
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "INSUFFICIENT_FUNDS", "message": "Insufficient funds"}}
    assert client.get(f"/accounts/{source_id}").json()["balance"] == 100
    assert client.get(f"/accounts/{destination_id}").json()["balance"] == 0
    with Session(bind=connection) as session:
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 0


@pytest.mark.parametrize("missing_source", [True, False])
def test_missing_account_returns_404(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], missing_source: bool
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    missing_id = 9223372036854775807
    response = post_transfer(client, {
        "source_account_id": missing_id if missing_source else source_id,
        "destination_account_id": destination_id if missing_source else missing_id,
        "amount": 10,
    })
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "ACCOUNT_NOT_FOUND", "message": "Account not found"}}
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0


def test_same_account_returns_400(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int]
) -> None:
    client, connection = database_client
    source_id, _ = funded_accounts
    response = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": source_id, "amount": 10
    })
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "SAME_ACCOUNT_TRANSFER", "message": "Accounts must differ"}}
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 100
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0


@pytest.mark.parametrize("failure_stage", [
    "account_locking", "balance_validation", "transfer_creation", "debit_history", "credit_history"
])
def test_failed_transfer_rolls_back_everything(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    lock_accounts = transfer_repository.lock_accounts
    create_transfer = transfer_repository.create_transfer
    create_entry = transfer_repository.create_account_transaction

    def fail_after_lock(session: Session, source_account_id: int, destination_account_id: int) -> None:
        lock_accounts(session, source_account_id, destination_account_id)
        raise RuntimeError("account locking failed")

    def fail_after_transfer(
        session: Session, source_account_id: int, destination_account_id: int, amount: int,
        idempotency_key: str | None, request_hash: str | None,
    ) -> None:
        create_transfer(session, source_account_id, destination_account_id, amount, idempotency_key, request_hash)
        raise RuntimeError("transfer creation failed")

    def fail_after_entry(session: Session, account_id: int, transfer_id: int, direction: str, amount: int) -> None:
        create_entry(session, account_id, transfer_id, direction, amount)
        if direction == failure_stage.removesuffix("_history"):
            raise RuntimeError("history creation failed")

    amount = 101 if failure_stage == "balance_validation" else 40
    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": amount}
    with monkeypatch.context() as patcher:
        if failure_stage == "account_locking":
            patcher.setattr(transfer_repository, "lock_accounts", fail_after_lock)
        elif failure_stage == "transfer_creation":
            patcher.setattr(transfer_repository, "create_transfer", fail_after_transfer)
        elif failure_stage in ("debit_history", "credit_history"):
            patcher.setattr(transfer_repository, "create_account_transaction", fail_after_entry)
        response = post_transfer(client, body, key="retry-after-rollback")
    expected_error = (
        {"code": "INSUFFICIENT_FUNDS", "message": "Insufficient funds"} if failure_stage == "balance_validation"
        else {"code": "INTERNAL_ERROR", "message": "Internal server error"}
    )
    assert response.status_code == (409 if failure_stage == "balance_validation" else 500)
    assert response.json() == {"error": expected_error}
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 0

    if failure_stage == "balance_validation":
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            session.get(Account, source_id).balance = amount
            session.commit()
    retry = post_transfer(client, body, key="retry-after-rollback")
    assert retry.status_code == 201
    assert client.get(f"/accounts/{source_id}").json()["balance"] == (0 if failure_stage == "balance_validation" else 60)
    assert client.get(f"/accounts/{destination_id}").json()["balance"] == amount
    with Session(bind=connection) as session:
        transfer = session.get(Transfer, retry.json()["id"])
        assert transfer.idempotency_key == "retry-after-rollback"
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 1
        entries = session.scalars(select(AccountTransaction).where(AccountTransaction.transfer_id == transfer.id)).all()
        assert {(entry.account_id, entry.direction, entry.amount) for entry in entries} == {
            (source_id, "debit", amount), (destination_id, "credit", amount)
        }


def test_duplicate_account_transaction_returns_safe_conflict(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, connection = database_client
    source_id, destination_id = funded_accounts
    create_entry = transfer_repository.create_account_transaction

    def duplicate_debit(session: Session, account_id: int, transfer_id: int, direction: str, amount: int) -> None:
        create_entry(session, account_id, transfer_id, direction, amount)
        if direction == "debit":
            create_entry(session, account_id, transfer_id, direction, amount)

    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": 10}
    with monkeypatch.context() as patcher:
        patcher.setattr(transfer_repository, "create_account_transaction", duplicate_debit)
        response = post_transfer(client, body, key="constraint-retry-key")
    assert response.status_code == 409
    assert response.json() == {"error": {
        "code": "ACCOUNT_TRANSACTION_CONFLICT", "message": "Account transaction already exists"
    }}
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(
            AccountTransaction.account_id.in_((source_id, destination_id))
        )) == 0

    retry = post_transfer(client, body, key="constraint-retry-key")
    assert retry.status_code == 201
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 90
        assert session.get(Account, destination_id).balance == 10
        transfer = session.get(Transfer, retry.json()["id"])
        assert transfer.idempotency_key == "constraint-retry-key"
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 1
        entries = session.scalars(select(AccountTransaction).where(AccountTransaction.transfer_id == transfer.id)).all()
        assert {(entry.account_id, entry.direction, entry.amount) for entry in entries} == {
            (source_id, "debit", 10), (destination_id, "credit", 10)
        }


def test_infrastructure_error_hides_connection_details(
    database_client: tuple[TestClient, Connection], funded_accounts: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = database_client
    source_id, destination_id = funded_accounts

    def fail_connection(session: Session, key: str) -> None:
        raise OperationalError("SELECT private_sql", {"password": "private_value"}, Exception("private_database_address"))

    monkeypatch.setattr(transfer_repository, "get_transfer_by_key", fail_connection)
    response = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": 10
    })
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "DATABASE_UNAVAILABLE", "message": "Database unavailable"}}
    assert all(secret not in response.text for secret in ("private_sql", "private_value", "private_database_address"))


def test_database_overflow_returns_sanitized_error(database_client: tuple[TestClient, Connection]) -> None:
    client, connection = database_client
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        source, destination = Account(balance=1), Account(balance=2**63 - 1)
        session.add_all((source, destination))
        session.commit()
        source_id, destination_id = source.id, destination.id
    response = post_transfer(client, {
        "source_account_id": source_id, "destination_account_id": destination_id, "amount": 1
    })
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "DATABASE_ERROR", "message": "Database operation failed"}}
    with Session(bind=connection) as session:
        assert session.get(Account, source_id).balance == 1
        assert session.get(Account, destination_id).balance == 2**63 - 1
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0


@pytest.fixture
def concurrent_transfer_accounts() -> Iterator[tuple[int, list[int]]]:
    with SessionLocal() as session:
        with session.begin():
            source = Account(balance=100)
            destinations = [Account() for _ in range(12)]
            session.add_all((source, *destinations))
            session.flush()
            source_id = source.id
            destination_ids = [account.id for account in destinations]
    try:
        yield source_id, destination_ids
    finally:
        with SessionLocal() as session:
            with session.begin():
                account_ids = [source_id, *destination_ids]
                transfer_ids = session.scalars(
                    select(Transfer.id).where(or_(
                        Transfer.source_account_id.in_(account_ids), Transfer.destination_account_id.in_(account_ids)
                    ))
                ).all()
                session.execute(delete(AccountTransaction).where(AccountTransaction.transfer_id.in_(transfer_ids)))
                session.execute(delete(Transfer).where(Transfer.id.in_(transfer_ids)))
                session.execute(delete(Account).where(Account.id.in_(account_ids)))


def test_concurrent_http_transfers_conserve_money(
    concurrent_transfer_accounts: tuple[int, list[int]]
) -> None:
    source_id, destination_ids = concurrent_transfer_accounts
    amount = 30
    with SessionLocal() as session:
        initial_balances = {account.id: account.balance for account in session.scalars(
            select(Account).where(Account.id.in_([source_id, *destination_ids]))
        )}
    assert initial_balances[source_id] == 100
    assert all(initial_balances[destination_id] == 0 for destination_id in destination_ids)
    initial_total = sum(initial_balances.values())
    assert len(destination_ids) * amount > initial_total
    start = Barrier(len(destination_ids))

    def attempt_transfer(destination_id: int) -> tuple[int, dict]:
        with TestClient(app) as client:
            start.wait(timeout=30)
            response = post_transfer(client, {
                "source_account_id": source_id, "destination_account_id": destination_id, "amount": amount
            }, key=f"destination-{destination_id}")
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=len(destination_ids)) as executor:
        futures = [executor.submit(attempt_transfer, destination_id) for destination_id in destination_ids]
        results = [(destination_id, future.result(timeout=60)) for destination_id, future in zip(destination_ids, futures)]

    successful = {destination_id: body for destination_id, (code, body) in results if code == 201}
    assert len(successful) == 3
    assert all(
        body["source_account_id"] == source_id
        and body["destination_account_id"] == destination_id
        and body["amount"] == amount
        for destination_id, body in successful.items()
    )
    assert sum(code == 409 for _, (code, _) in results) == 9
    assert all(body == {"error": {"code": "INSUFFICIENT_FUNDS", "message": "Insufficient funds"}}
               for _, (code, body) in results if code == 409)

    with SessionLocal() as session:
        balances = {account.id: account.balance for account in session.scalars(
            select(Account).where(Account.id.in_([source_id, *destination_ids]))
        )}
        transfers = session.scalars(select(Transfer).where(Transfer.source_account_id == source_id)).all()
        transfer_ids = [transfer.id for transfer in transfers]
        entries = session.scalars(select(AccountTransaction).where(
            AccountTransaction.transfer_id.in_(transfer_ids)
        )).all()

        assert len(balances) == 1 + len(destination_ids)
        assert all(balance >= 0 for balance in balances.values())
        assert balances[source_id] == initial_total - len(successful) * amount == 10
        assert all(balances[destination_id] == (amount if destination_id in successful else 0)
                   for destination_id in destination_ids)
        assert sum(balances.values()) == initial_total

        assert len(transfers) == len(successful)
        assert {transfer.id for transfer in transfers} == {body["id"] for body in successful.values()}
        assert {transfer.destination_account_id: transfer.amount for transfer in transfers} == {
            destination_id: amount for destination_id in successful
        }
        assert sum(transfer.amount for transfer in transfers) == initial_total - balances[source_id]
        assert len(entries) == 2 * len(successful)
        expected_entries = Counter(
            (transfer.id, account_id, direction, transfer.amount)
            for transfer in transfers
            for account_id, direction in ((source_id, "debit"), (transfer.destination_account_id, "credit"))
        )
        assert Counter((entry.transfer_id, entry.account_id, entry.direction, entry.amount) for entry in entries) == expected_entries


def test_concurrent_identical_requests_create_one_transfer(
    concurrent_transfer_accounts: tuple[int, list[int]]
) -> None:
    source_id, destination_ids = concurrent_transfer_accounts
    destination_id = destination_ids[0]
    start = Barrier(12)

    def attempt_transfer() -> tuple[int, dict]:
        with TestClient(app) as client:
            start.wait(timeout=30)
            response = post_transfer(client, {
                "source_account_id": source_id, "destination_account_id": destination_id, "amount": 80
            }, key="identical-concurrent-key")
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(attempt_transfer) for _ in range(12)]
        results = [future.result(timeout=60) for future in futures]

    assert sorted(code for code, _ in results) == [200] * 11 + [201]
    assert all(body == results[0][1] for _, body in results)
    with SessionLocal() as session:
        assert session.get(Account, source_id).balance == 20
        assert session.get(Account, destination_id).balance == 80
        assert sum(session.get(Account, account_id).balance for account_id in [source_id, *destination_ids]) == 100
        transfers = session.scalars(select(Transfer).where(Transfer.idempotency_key == "identical-concurrent-key")).all()
        assert len(transfers) == 1
        assert transfers[0].id == results[0][1]["id"]
        entries = session.scalars(select(AccountTransaction).where(AccountTransaction.transfer_id == transfers[0].id)).all()
        assert {(entry.account_id, entry.direction, entry.amount) for entry in entries} == {
            (source_id, "debit", 80), (destination_id, "credit", 80)
        }


def test_duplicate_requests_wait_for_postgres_account_lock(
    concurrent_transfer_accounts: tuple[int, list[int]]
) -> None:
    source_id, destination_ids = concurrent_transfer_accounts
    destination_id = destination_ids[0]
    arrived = Event()
    counter_lock = Lock()
    waiting_queries = 0
    start = Barrier(2)

    def record_lock_query(connection: Connection, cursor: object, statement: str, parameters: object,
                          context: object, many: bool) -> None:
        nonlocal waiting_queries
        if "FOR UPDATE" in statement and isinstance(parameters, dict) and source_id in parameters.values():
            with counter_lock:
                waiting_queries += 1
                if waiting_queries == 2:
                    arrived.set()

    def attempt_transfer() -> tuple[int, dict]:
        with TestClient(app) as client:
            start.wait(timeout=30)
            response = post_transfer(client, {
                "source_account_id": source_id, "destination_account_id": destination_id, "amount": 80
            }, key="contended-retry-key")
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        with SessionLocal() as blocker:
            with blocker.begin():
                blocker.execute(select(Account).where(Account.id == source_id).with_for_update())
                event.listen(engine, "before_cursor_execute", record_lock_query)
                try:
                    first = executor.submit(attempt_transfer)
                    second = executor.submit(attempt_transfer)
                    assert arrived.wait(timeout=30)
                    assert not first.done() and not second.done()
                finally:
                    event.remove(engine, "before_cursor_execute", record_lock_query)

        results = [first.result(timeout=30), second.result(timeout=30)]
    assert sorted(code for code, _ in results) == [200, 201]
    assert results[0][1] == results[1][1]
    with SessionLocal() as session:
        assert session.get(Account, source_id).balance == 20
        assert session.get(Account, destination_id).balance == 80
        transfers = session.scalars(select(Transfer).where(Transfer.idempotency_key == "contended-retry-key")).all()
        assert len(transfers) == 1
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(
            AccountTransaction.transfer_id == transfers[0].id
        )) == 2


def test_committed_transfer_replayed_after_response_is_lost(
    concurrent_transfer_accounts: tuple[int, list[int]]
) -> None:
    source_id, destination_ids = concurrent_transfer_accounts
    destination_id = destination_ids[0]
    body = {"source_account_id": source_id, "destination_account_id": destination_id, "amount": 100}
    with TestClient(app) as first_client:
        first_response = post_transfer(first_client, body, key="lost-response-key")
        assert first_response.status_code == 201
        created_id = first_response.json()["id"]

    with TestClient(app) as retry_client:
        replay = post_transfer(retry_client, body, key="lost-response-key")
        assert replay.status_code == 200
        assert replay.json()["id"] == created_id

    with SessionLocal() as session:
        assert session.get(Account, source_id).balance == 0
        assert session.get(Account, destination_id).balance == 100
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.idempotency_key == "lost-response-key")) == 1
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(
            AccountTransaction.transfer_id == created_id
        )) == 2


def test_unique_constraint_resolves_different_account_race(
    concurrent_transfer_accounts: tuple[int, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source_id, destination_ids = concurrent_transfer_accounts
    first_destination_id, second_source_id, second_destination_id = destination_ids[:3]
    with SessionLocal() as session:
        with session.begin():
            session.get(Account, second_source_id).balance = 100

    start = Barrier(2)
    at_insert = Barrier(2)
    create_transfer = transfer_repository.create_transfer
    unique_violations = []

    def synchronized_insert(
        session: Session, source_account_id: int, destination_account_id: int, amount: int,
        idempotency_key: str | None = None, request_hash: str | None = None,
    ) -> Transfer:
        at_insert.wait(timeout=30)
        return create_transfer(session, source_account_id, destination_account_id, amount, idempotency_key, request_hash)

    def record_error(context: object) -> None:
        original = context.original_exception
        if getattr(original, "sqlstate", None) == "23505":
            unique_violations.append(original.diag.constraint_name)

    monkeypatch.setattr(transfer_repository, "create_transfer", synchronized_insert)
    event.listen(engine, "handle_error", record_error)
    try:
        def attempt_transfer(source_id: int, destination_id: int) -> tuple[int, dict]:
            with TestClient(app) as client:
                start.wait(timeout=30)
                response = post_transfer(client, {
                    "source_account_id": source_id, "destination_account_id": destination_id, "amount": 40
                }, key="racing-different-requests")
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(attempt_transfer, first_source_id, first_destination_id)
            second = executor.submit(attempt_transfer, second_source_id, second_destination_id)
            results = [first.result(timeout=60), second.result(timeout=60)]
    finally:
        event.remove(engine, "handle_error", record_error)

    with SessionLocal() as session:
        observed = session.execute(select(Transfer.id, Transfer.source_account_id).where(
            Transfer.idempotency_key == "racing-different-requests"
        )).all()
    assert sorted(code for code, _ in results) == [201, 409], (results, unique_violations, observed)
    assert unique_violations == ["uq_transfers_idempotency_key"]
    assert next(body for code, body in results if code == 409) == {"error": {
        "code": "IDEMPOTENCY_KEY_REUSED", "message": "Idempotency key already used for a different transfer"
    }}
    with SessionLocal() as session:
        transfers = session.scalars(select(Transfer).where(Transfer.idempotency_key == "racing-different-requests")).all()
        assert len(transfers) == 1
        winner = transfers[0]
        assert winner.id == next(body["id"] for code, body in results if code == 201)
        assert session.get(Account, winner.source_account_id).balance == 60
        assert session.get(Account, winner.destination_account_id).balance == 40
        other_source_id = second_source_id if winner.source_account_id == first_source_id else first_source_id
        other_destination_id = second_destination_id if winner.source_account_id == first_source_id else first_destination_id
        assert session.get(Account, other_source_id).balance == 100
        assert session.get(Account, other_destination_id).balance == 0
        assert sum(session.get(Account, account_id).balance for account_id in [first_source_id, *destination_ids]) == 200
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(
            AccountTransaction.transfer_id == winner.id
        )) == 2