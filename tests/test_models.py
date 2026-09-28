from collections.abc import Iterator

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Account, AccountTransaction, Transfer


@pytest.fixture
def session() -> Iterator[Session]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                yield session
        finally:
            transaction.rollback()


@pytest.fixture
def accounts(session: Session) -> tuple[Account, Account]:
    source = Account(balance=100)
    destination = Account(balance=0)
    session.add_all((source, destination))
    session.flush()
    return source, destination


@pytest.fixture
def transfer(session: Session, accounts: tuple[Account, Account]) -> Transfer:
    source, destination = accounts
    transfer = Transfer(source_account_id=source.id, destination_account_id=destination.id, amount=10)
    session.add(transfer)
    session.flush()
    return transfer


def test_account_defaults_and_nonnegative_balance(session: Session) -> None:
    account = Account()
    session.add(account)
    session.flush()
    assert account.id is not None
    assert account.balance == 0
    assert account.created_at.tzinfo is not None
    assert account.updated_at.tzinfo is not None

    account.balance = -1
    with pytest.raises(IntegrityError) as error:
        session.flush()
    assert error.value.orig.diag.constraint_name == "ck_accounts_balance_nonnegative"


@pytest.mark.parametrize(("amount", "same_account"), [(0, False), (-1, False), (10, True)])
def test_transfer_checks(
    session: Session, accounts: tuple[Account, Account], amount: int, same_account: bool
) -> None:
    source, destination = accounts
    session.add(Transfer(
        source_account_id=source.id,
        destination_account_id=source.id if same_account else destination.id,
        amount=amount,
    ))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    expected = "ck_transfers_distinct_accounts" if same_account else "ck_transfers_amount_positive"
    assert error.value.orig.diag.constraint_name == expected


@pytest.mark.parametrize("missing_source", [True, False])
def test_transfer_accounts_must_exist(
    session: Session, accounts: tuple[Account, Account], missing_source: bool
) -> None:
    source, destination = accounts
    session.add(Transfer(
        source_account_id=-1 if missing_source else source.id,
        destination_account_id=destination.id if missing_source else -1,
        amount=10,
    ))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    expected = "transfers_source_account_id_fkey" if missing_source else "transfers_destination_account_id_fkey"
    assert error.value.orig.diag.constraint_name == expected


@pytest.mark.parametrize("delete_source", [True, False])
def test_transfer_references_prevent_account_deletion(
    session: Session, accounts: tuple[Account, Account], transfer: Transfer, delete_source: bool
) -> None:
    source, destination = accounts
    account = source if delete_source else destination
    with pytest.raises(IntegrityError) as error:
        with session.begin_nested():
            session.delete(account)
            session.flush()
    expected = "transfers_source_account_id_fkey" if delete_source else "transfers_destination_account_id_fkey"
    assert error.value.orig.diag.constraint_name == expected


@pytest.mark.parametrize(
    ("direction", "amount", "missing_account", "missing_transfer"),
    [
        ("other", 10, False, False),
        ("debit", 0, False, False),
        ("debit", 10, True, False),
        ("debit", 10, False, True),
    ],
)
def test_account_transaction_checks(
    session: Session,
    accounts: tuple[Account, Account],
    transfer: Transfer,
    direction: str,
    amount: int,
    missing_account: bool,
    missing_transfer: bool,
) -> None:
    source, _ = accounts
    session.add(AccountTransaction(
        account_id=-1 if missing_account else source.id,
        transfer_id=-1 if missing_transfer else transfer.id,
        direction=direction,
        amount=amount,
    ))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    expected = (
        "account_transactions_account_id_fkey" if missing_account else
        "account_transactions_transfer_id_fkey" if missing_transfer else
        "ck_account_transactions_amount_positive" if amount == 0 else
        "ck_account_transactions_direction"
    )
    assert error.value.orig.diag.constraint_name == expected


def test_duplicate_transfer_direction_is_rejected(
    session: Session, accounts: tuple[Account, Account], transfer: Transfer
) -> None:
    source, _ = accounts
    session.add(AccountTransaction(account_id=source.id, transfer_id=transfer.id, direction="debit", amount=10))
    session.flush()
    session.add(AccountTransaction(account_id=source.id, transfer_id=transfer.id, direction="debit", amount=10))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    assert error.value.orig.diag.constraint_name == "uq_account_transactions_transfer_direction"


def test_account_transaction_reference_prevents_account_deletion(session: Session, transfer: Transfer) -> None:
    account = Account()
    session.add(account)
    session.flush()
    session.add(AccountTransaction(account_id=account.id, transfer_id=transfer.id, direction="debit", amount=10))
    session.flush()
    with pytest.raises(IntegrityError) as error:
        with session.begin_nested():
            session.delete(account)
            session.flush()
    assert error.value.orig.diag.constraint_name == "account_transactions_account_id_fkey"


def test_account_transaction_reference_prevents_transfer_deletion(
    session: Session, accounts: tuple[Account, Account], transfer: Transfer
) -> None:
    source, _ = accounts
    session.add(AccountTransaction(account_id=source.id, transfer_id=transfer.id, direction="debit", amount=10))
    session.flush()
    with pytest.raises(IntegrityError) as error:
        with session.begin_nested():
            session.delete(transfer)
            session.flush()
    assert error.value.orig.diag.constraint_name == "account_transactions_transfer_id_fkey"


def test_history_index_orders_newest_first(session: Session) -> None:
    indexes = {index["name"]: index for index in inspect(session.bind).get_indexes("account_transactions")}
    history_index = indexes["ix_account_transactions_account_id_created_at_id_desc"]
    assert history_index["column_names"] == ["account_id", "created_at", "id"]
    assert history_index["column_sorting"] == {"created_at": ("desc",), "id": ("desc",)}
    assert "ix_account_transactions_transfer_id" not in indexes
    assert any(
        constraint["name"] == "uq_account_transactions_transfer_direction"
        and constraint["column_names"] == ["transfer_id", "direction"]
        for constraint in inspect(session.bind).get_unique_constraints("account_transactions")
    )


def test_historical_transfers_can_have_null_idempotency_fields(
    session: Session, accounts: tuple[Account, Account]
) -> None:
    source, destination = accounts
    transfers = [
        Transfer(source_account_id=source.id, destination_account_id=destination.id, amount=amount)
        for amount in (10, 20)
    ]
    session.add_all(transfers)
    session.flush()
    assert all(transfer.id is not None for transfer in transfers)
    assert all(transfer.idempotency_key is None and transfer.request_hash is None for transfer in transfers)


def test_transfer_idempotency_key_is_unique(session: Session, accounts: tuple[Account, Account]) -> None:
    source, destination = accounts
    session.add(Transfer(
        source_account_id=source.id, destination_account_id=destination.id, amount=10,
        idempotency_key="same-key", request_hash="a" * 64,
    ))
    session.flush()
    session.add(Transfer(
        source_account_id=source.id, destination_account_id=destination.id, amount=20,
        idempotency_key="same-key", request_hash="b" * 64,
    ))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    assert error.value.orig.diag.constraint_name == "uq_transfers_idempotency_key"


@pytest.mark.parametrize(
    ("key", "request_hash"),
    [("key-only", None), (None, "a" * 64)],
)
def test_transfer_idempotency_fields_must_be_paired(
    session: Session, accounts: tuple[Account, Account], key: str | None, request_hash: str | None
) -> None:
    source, destination = accounts
    session.add(Transfer(
        source_account_id=source.id, destination_account_id=destination.id, amount=10,
        idempotency_key=key, request_hash=request_hash,
    ))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    assert error.value.orig.diag.constraint_name == "ck_transfers_idempotency_pair"