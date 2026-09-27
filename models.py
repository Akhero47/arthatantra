from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_accounts_balance_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_transfers_amount_positive"),
        CheckConstraint("source_account_id <> destination_account_id", name="ck_transfers_distinct_accounts"),
        CheckConstraint("(idempotency_key IS NULL) = (request_hash IS NULL)", name="ck_transfers_idempotency_pair"),
        UniqueConstraint("idempotency_key", name="uq_transfers_idempotency_key"),
        Index("ix_transfers_source_account_id", "source_account_id"),
        Index("ix_transfers_destination_account_id", "destination_account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    destination_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AccountTransaction(Base):
    __tablename__ = "account_transactions"
    __table_args__ = (
        CheckConstraint("direction IN ('debit', 'credit')", name="ck_account_transactions_direction"),
        CheckConstraint("amount > 0", name="ck_account_transactions_amount_positive"),
        UniqueConstraint("transfer_id", "direction", name="uq_account_transactions_transfer_direction"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    transfer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("transfers.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


Index(
    "ix_account_transactions_account_id_created_at_id_desc",
    AccountTransaction.account_id, AccountTransaction.created_at.desc(), AccountTransaction.id.desc(),
)