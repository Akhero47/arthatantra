from sqlalchemy.orm import Session

import app.account.account_repository as account_repository
from app.models import Account, AccountTransaction


class AccountNotFoundError(Exception):
    pass


def create_account(session: Session) -> Account:
    with session.begin():
        account = account_repository.create_account(session)
    return account


def get_account(session: Session, account_id: int) -> Account:
    account = account_repository.get_account(session, account_id)
    if account is None:
        raise AccountNotFoundError()
    return account


def get_transaction_history(
    session: Session, account_id: int, page: int, limit: int
) -> tuple[list[AccountTransaction], int]:
    get_account(session, account_id)
    total = account_repository.count_account_transactions(session, account_id)
    offset = (page - 1) * limit
    if offset >= total:
        return [], total
    entries = account_repository.list_account_transactions(session, account_id, limit, offset)
    return entries, total