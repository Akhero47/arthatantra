from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, AccountTransaction


def create_account(session: Session) -> Account:
    account = Account()
    session.add(account)
    session.flush()
    return account


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def count_account_transactions(session: Session, account_id: int) -> int:
    statement = select(func.count()).select_from(AccountTransaction).where(AccountTransaction.account_id == account_id)
    return session.scalar(statement) or 0


def list_account_transactions(session: Session, account_id: int, limit: int, offset: int) -> list[AccountTransaction]:
    statement = (
        select(AccountTransaction)
        .where(AccountTransaction.account_id == account_id)
        .order_by(AccountTransaction.created_at.desc(), AccountTransaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement))