from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, AccountTransaction, Transfer


class DuplicateIdempotencyKey(Exception):
    pass


def lock_accounts(session: Session, source_account_id: int, destination_account_id: int) -> dict[int, Account]:
    accounts = {}
    for account_id in sorted((source_account_id, destination_account_id)):
        account = session.scalar(
            select(Account).where(Account.id == account_id).with_for_update().execution_options(populate_existing=True)
        )
        if account is not None:
            accounts[account_id] = account
    return accounts


def update_balances(session: Session, source: Account, destination: Account, source_balance: int, destination_balance: int) -> None:
    source.balance = source_balance
    destination.balance = destination_balance
    session.flush()


def get_transfer_by_key(session: Session, idempotency_key: str) -> Transfer | None:
    return session.scalar(select(Transfer).where(Transfer.idempotency_key == idempotency_key))


def create_transfer(
    session: Session, source_account_id: int, destination_account_id: int, amount: int,
    idempotency_key: str | None = None, request_hash: str | None = None,
) -> Transfer:
    transfer = Transfer(
        source_account_id=source_account_id, destination_account_id=destination_account_id, amount=amount,
        idempotency_key=idempotency_key, request_hash=request_hash,
    )
    session.add(transfer)
    try:
        session.flush()
    except IntegrityError as exc:
        if idempotency_key is not None and getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == "uq_transfers_idempotency_key":
            raise DuplicateIdempotencyKey() from exc
        raise
    return transfer


def create_account_transaction(
    session: Session, account_id: int, transfer_id: int, direction: str, amount: int
) -> AccountTransaction:
    transaction = AccountTransaction(account_id=account_id, transfer_id=transfer_id, direction=direction, amount=amount)
    session.add(transaction)
    session.flush()
    return transaction