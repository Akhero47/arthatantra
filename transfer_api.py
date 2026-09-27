from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

import transfer_service
from api_errors import ErrorResponse
from db import get_db
from models import Transfer
from transfer_schemas import TransferCreate, TransferResponse


router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.post(
    "",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Transfer funds between accounts",
    description=(
        "Moves a positive integer amount of minor units between two distinct accounts. "
        "The required Idempotency-Key is globally unique and case-sensitive: the first successful request "
        "creates one transfer (201), and retrying the same key with identical source, destination, and amount "
        "returns that transfer (200) without moving money again. Reusing a key with different transfer fields "
        "returns 409. Failed requests do not reserve keys, so they can be retried."
    ),
    response_description="New transfer with its ID, accounts, amount in minor units, and creation time.",
    responses={
        200: {"model": TransferResponse, "description": "Matching-key replay; original transfer returned without another debit."},
        400: {"model": ErrorResponse, "description": "Source and destination are the same (SAME_ACCOUNT_TRANSFER)."},
        404: {"model": ErrorResponse, "description": "Source or destination account does not exist (ACCOUNT_NOT_FOUND)."},
        409: {"model": ErrorResponse, "description": "Insufficient funds (INSUFFICIENT_FUNDS) or key reused for different fields (IDEMPOTENCY_KEY_REUSED)."},
        422: {"model": ErrorResponse, "description": "Invalid body or Idempotency-Key (VALIDATION_ERROR or INVALID_AMOUNT)."},
    },
)
def create_transfer(
    body: TransferCreate,
    response: Response,
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=1, max_length=255, pattern=r"^[!-~]+$",
        description="Required 1-255 visible ASCII characters without whitespace; reuse only for the same transfer request.",
    )],
    session: Session = Depends(get_db),
) -> Transfer:
    transfer, created = transfer_service.transfer_money(
        session, body.source_account_id, body.destination_account_id, body.amount, idempotency_key
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return transfer