from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AccountCreate(BaseModel):
    """Create an account with a zero balance; no input fields are accepted."""

    model_config = ConfigDict(extra="forbid")


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Database-generated account ID")
    balance: int = Field(description="Current balance in integer minor units")
    created_at: datetime = Field(description="Account creation time with timezone")
    updated_at: datetime = Field(description="Time of the most recent SQLAlchemy account update")


class AccountTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Ledger entry ID")
    account_id: int = Field(description="Account whose history contains this entry")
    transfer_id: int = Field(description="Transfer that created this entry")
    direction: Literal["debit", "credit"] = Field(description="Debit removes funds; credit adds funds")
    amount: int = Field(description="Positive amount in integer minor units")
    created_at: datetime = Field(description="Ledger entry creation time with timezone")


class AccountTransactionPage(BaseModel):
    items: list[AccountTransactionResponse] = Field(description="Entries ordered by created_at descending, then ID descending")
    page: int = Field(description="One-based page number")
    limit: int = Field(description="Maximum entries requested per page")
    total: int = Field(description="Total ledger entries for this account at count-query time")