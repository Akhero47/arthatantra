from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt


AccountId = Annotated[StrictInt, Field(gt=0, le=2**63 - 1, description="Existing account ID")]
TransferAmount = Annotated[StrictInt, Field(
    gt=0, le=2**63 - 1, description="Positive integer amount in minor units; no floating-point money"
)]


class TransferCreate(BaseModel):
    """Transfer funds between two different existing accounts."""

    model_config = ConfigDict(extra="forbid")

    source_account_id: AccountId
    destination_account_id: AccountId
    amount: TransferAmount


class TransferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Database-generated transfer ID")
    source_account_id: int = Field(description="Account debited by this transfer")
    destination_account_id: int = Field(description="Account credited by this transfer")
    amount: int = Field(description="Transferred amount in integer minor units")
    created_at: datetime = Field(description="Transfer creation time with timezone")