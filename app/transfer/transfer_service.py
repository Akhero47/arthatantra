from sqlalchemy.orm import Session

import app.transfer.transfer_repository as transfer_repository
from app.models import Transfer
from app.transfer.transfer_request_hash import hash_transfer_request


class InvalidTransferAmountError(Exception):
    pass


class SameAccountError(Exception):
    pass


class TransferAccountNotFoundError(Exception):
    def __init__(self, account_id: int) -> None:
        self.account_id = account_id
        super().__init__(f"Account {account_id} not found")


class InsufficientFundsError(Exception):
    pass


class IdempotencyKeyConflictError(Exception):
    pass


def get_matching_transfer(session: Session, idempotency_key: str, request_hash: str) -> Transfer | None:
    existing = transfer_repository.get_transfer_by_key(session, idempotency_key)
    if existing is not None and existing.request_hash != request_hash:
        raise IdempotencyKeyConflictError()
    return existing


def transfer_money(
    session: Session, source_account_id: int, destination_account_id: int, amount: int,
    idempotency_key: str | None = None,
) -> tuple[Transfer, bool]:
    if not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount <= 2**63 - 1:
        raise InvalidTransferAmountError()

    request_hash = hash_transfer_request(source_account_id, destination_account_id, amount) if idempotency_key else None
    try:
        with session.begin():
            if idempotency_key is not None:
                existing = get_matching_transfer(session, idempotency_key, request_hash)
                if existing is not None:
                    return existing, False
            if source_account_id == destination_account_id:
                raise SameAccountError()

            accounts = transfer_repository.lock_accounts(session, source_account_id, destination_account_id)
            if idempotency_key is not None:
                existing = get_matching_transfer(session, idempotency_key, request_hash)
                if existing is not None:
                    return existing, False
            if source_account_id not in accounts:
                raise TransferAccountNotFoundError(source_account_id)
            if destination_account_id not in accounts:
                raise TransferAccountNotFoundError(destination_account_id)

            source = accounts[source_account_id]
            destination = accounts[destination_account_id]
            if source.balance < amount:
                raise InsufficientFundsError()

            transfer_repository.update_balances(
                session, source, destination, source.balance - amount, destination.balance + amount
            )
            transfer = transfer_repository.create_transfer(
                session, source_account_id, destination_account_id, amount, idempotency_key, request_hash
            )
            transfer_repository.create_account_transaction(session, source_account_id, transfer.id, "debit", amount)
            transfer_repository.create_account_transaction(session, destination_account_id, transfer.id, "credit", amount)
        return transfer, True
    except transfer_repository.DuplicateIdempotencyKey:
        if idempotency_key is None:
            raise
        with session.begin():
            existing = get_matching_transfer(session, idempotency_key, request_hash)
            if existing is None:
                raise
            return existing, False