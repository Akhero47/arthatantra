from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import delete, event, func, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

import transfer_repository
from db import SessionLocal, engine
from models import Account, AccountTransaction, Transfer
from transfer_service import (
    InsufficientFundsError,
    InvalidTransferAmountError,
    SameAccountError,
    TransferAccountNotFoundError,
    transfer_money,
)


@pytest.fixture
def account_pair() -> Iterator[tuple[Connection, int, int]]:
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                source = Account(balance=100)
                destination = Account(balance=0)
                session.add_all((source, destination))
                session.commit()
                source_id, destination_id = source.id, destination.id
            yield connection, source_id, destination_id
        finally:
            outer_transaction.rollback()


def test_successful_transfer_updates_balances_and_creates_records(
    account_pair: tuple[Connection, int, int]
) -> None:
    connection, source_id, destination_id = account_pair
    with Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as session:
        transfer, created = transfer_money(session, source_id, destination_id, 40)
        assert created is True
        assert (transfer.source_account_id, transfer.destination_account_id, transfer.amount) == (
            source_id, destination_id, 40
        )
        assert (session.get(Account, source_id).balance, session.get(Account, destination_id).balance) == (60, 40)
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.id == transfer.id)) == 1
        entries = session.scalars(
            select(AccountTransaction).where(AccountTransaction.transfer_id == transfer.id)
        ).all()
        assert {(entry.account_id, entry.direction, entry.amount) for entry in entries} == {
            (source_id, "debit", 40), (destination_id, "credit", 40)
        }
        assert sum(session.get(Account, account_id).balance for account_id in (source_id, destination_id)) == 100


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, 2**63])
def test_invalid_amount_leaves_accounts_untouched(
    account_pair: tuple[Connection, int, int], amount: int
) -> None:
    connection, source_id, destination_id = account_pair
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        with pytest.raises(InvalidTransferAmountError):
            transfer_money(session, source_id, destination_id, amount)
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0


def test_same_account_is_rejected(account_pair: tuple[Connection, int, int]) -> None:
    connection, source_id, _ = account_pair
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        with pytest.raises(SameAccountError):
            transfer_money(session, source_id, source_id, 10)
        assert session.get(Account, source_id).balance == 100
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0


@pytest.mark.parametrize("missing_source", [True, False])
def test_missing_account_rolls_back(
    account_pair: tuple[Connection, int, int], missing_source: bool
) -> None:
    connection, source_id, destination_id = account_pair
    missing_id = -1
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        with pytest.raises(TransferAccountNotFoundError) as error:
            transfer_money(
                session,
                missing_id if missing_source else source_id,
                destination_id if missing_source else missing_id,
                10,
            )
        assert error.value.account_id == missing_id
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0


def test_insufficient_funds_does_not_write(account_pair: tuple[Connection, int, int]) -> None:
    connection, source_id, destination_id = account_pair
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        with pytest.raises(InsufficientFundsError):
            transfer_money(session, source_id, destination_id, 101)
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 0


def test_late_failure_rolls_back_balances_and_ledger(
    account_pair: tuple[Connection, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, source_id, destination_id = account_pair
    create_entry = transfer_repository.create_account_transaction

    def fail_on_credit(session: Session, account_id: int, transfer_id: int, direction: str, amount: int) -> None:
        if direction == "credit":
            raise RuntimeError("credit entry failed")
        create_entry(session, account_id, transfer_id, direction, amount)

    monkeypatch.setattr(transfer_repository, "create_account_transaction", fail_on_credit)
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        with pytest.raises(RuntimeError, match="credit entry failed"):
            transfer_money(session, source_id, destination_id, 40)
        assert session.get(Account, source_id).balance == 100
        assert session.get(Account, destination_id).balance == 0
        assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.source_account_id == source_id)) == 0
        assert session.scalar(select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id.in_((source_id, destination_id)))) == 0


def test_reversed_accounts_are_locked_in_id_order(account_pair: tuple[Connection, int, int]) -> None:
    connection, first_id, second_id = account_pair
    locked_ids = []

    def record_lock(connection: Connection, cursor: object, statement: str, parameters: dict, context: object, many: bool) -> None:
        if "FOR UPDATE" in statement:
            locked_ids.append(parameters["id_1"])

    event.listen(connection, "before_cursor_execute", record_lock)
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            with session.begin():
                transfer_repository.lock_accounts(session, second_id, first_id)
    finally:
        event.remove(connection, "before_cursor_execute", record_lock)

    assert locked_ids == sorted((first_id, second_id))


@pytest.fixture
def committed_accounts() -> Iterator[tuple[int, int, int]]:
    with SessionLocal() as session:
        with session.begin():
            source = Account(balance=100)
            destinations = (Account(), Account())
            session.add_all((source, *destinations))
            session.flush()
            account_ids = (source.id, *(account.id for account in destinations))
    try:
        yield account_ids
    finally:
        with SessionLocal() as session:
            with session.begin():
                transfer_ids = session.scalars(
                    select(Transfer.id).where(
                        or_(Transfer.source_account_id.in_(account_ids), Transfer.destination_account_id.in_(account_ids))
                    )
                ).all()
                session.execute(delete(AccountTransaction).where(AccountTransaction.transfer_id.in_(transfer_ids)))
                session.execute(delete(Transfer).where(Transfer.id.in_(transfer_ids)))
                session.execute(delete(Account).where(Account.id.in_(account_ids)))


def test_concurrent_transfers_cannot_overspend(committed_accounts: tuple[int, int, int]) -> None:
    source_id, first_destination_id, second_destination_id = committed_accounts
    start = Barrier(2)

    def attempt_transfer(destination_id: int) -> str:
        with SessionLocal() as session:
            start.wait(timeout=10)
            try:
                transfer_money(session, source_id, destination_id, 80)
                return "success"
            except InsufficientFundsError:
                return "insufficient funds"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attempt_transfer, first_destination_id)
        second = executor.submit(attempt_transfer, second_destination_id)
        assert sorted((first.result(timeout=15), second.result(timeout=15))) == ["insufficient funds", "success"]

    with SessionLocal() as session:
        balances = [session.get(Account, account_id).balance for account_id in committed_accounts]
        assert balances[0] == 20
        assert sorted(balances[1:]) == [0, 80]
        assert sum(balances) == 100
        transfers = session.scalars(select(Transfer).where(Transfer.source_account_id == source_id)).all()
        assert len(transfers) == 1
        entries = session.scalars(select(AccountTransaction).where(AccountTransaction.transfer_id == transfers[0].id)).all()
        assert len(entries) == 2
        assert {(entry.direction, entry.amount) for entry in entries} == {("debit", 80), ("credit", 80)}


def test_concurrent_credits_do_not_lose_money(committed_accounts: tuple[int, int, int]) -> None:
    first_source_id, second_source_id, destination_id = committed_accounts
    with SessionLocal() as session:
        with session.begin():
            session.get(Account, second_source_id).balance = 100

    start = Barrier(2)

    def attempt_transfer(source_id: int, amount: int) -> None:
        with SessionLocal() as session:
            start.wait(timeout=10)
            transfer_money(session, source_id, destination_id, amount)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attempt_transfer, first_source_id, 70)
        second = executor.submit(attempt_transfer, second_source_id, 50)
        first.result(timeout=15)
        second.result(timeout=15)

    with SessionLocal() as session:
        balances = [session.get(Account, account_id).balance for account_id in committed_accounts]
        assert balances == [30, 50, 120]
        assert sum(balances) == 200
        transfers = session.scalars(select(Transfer).where(Transfer.destination_account_id == destination_id)).all()
        assert len(transfers) == 2
        entries = session.scalars(select(AccountTransaction).where(
            AccountTransaction.transfer_id.in_([transfer.id for transfer in transfers])
        )).all()
        assert len(entries) == 4