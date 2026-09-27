from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, status
from sqlalchemy.orm import Session

import account_service
from account_schemas import AccountCreate, AccountResponse, AccountTransactionPage
from api_errors import ErrorResponse
from db import get_db
from models import Account


router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a zero-balance account",
    description="Creates an account with a database-generated ID. No initial funding or other input fields are accepted.",
    response_description="The new account with a zero balance in minor units.",
    responses={422: {"model": ErrorResponse, "description": "Invalid request body (VALIDATION_ERROR)."}},
)
def create_account(
    body: Annotated[AccountCreate | None, Body(description="Omit the body or send an empty object; balance is not accepted.")] = None,
    session: Session = Depends(get_db),
) -> Account:
    return account_service.create_account(session)


@router.get(
    "/{account_id}",
    response_model=AccountResponse,
    summary="Get an account",
    response_description="Current balance and account timestamps.",
    responses={
        404: {"model": ErrorResponse, "description": "Account does not exist (ACCOUNT_NOT_FOUND)."},
        422: {"model": ErrorResponse, "description": "Invalid account ID (VALIDATION_ERROR)."},
    },
)
def get_account(
    account_id: Annotated[int, Path(gt=0, le=2**63 - 1, description="Positive database-generated account ID")],
    session: Session = Depends(get_db),
) -> Account:
    return account_service.get_account(session, account_id)


@router.get(
    "/{account_id}/transactions",
    response_model=AccountTransactionPage,
    summary="List an account's transactions",
    description=(
        "Returns debit (money leaving) and credit (money arriving) ledger entries newest first, "
        "ordered by creation time then ID descending. Uses one-based pages; a page beyond the end has no items."
    ),
    response_description="Account entries and a total counted when the request is processed.",
    responses={
        404: {"model": ErrorResponse, "description": "Account does not exist (ACCOUNT_NOT_FOUND)."},
        422: {"model": ErrorResponse, "description": "Invalid ID, page, or limit (VALIDATION_ERROR)."},
    },
)
def get_account_transactions(
    account_id: Annotated[int, Path(gt=0, le=2**63 - 1, description="Account whose ledger entries are requested")],
    page: Annotated[int, Query(ge=1, description="One-based page number")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Entries per page, from 1 to 100")] = 20,
    session: Session = Depends(get_db),
) -> AccountTransactionPage:
    entries, total = account_service.get_transaction_history(session, account_id, page, limit)
    return AccountTransactionPage(items=entries, page=page, limit=limit, total=total)