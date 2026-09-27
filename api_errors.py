from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException

import account_service
import transfer_service


class ValidationIssue(BaseModel):
    location: list[str | int] = Field(description="Request path to the invalid field")
    message: str = Field(description="Safe explanation without the submitted value")
    type: str = Field(description="Validation rule that failed")


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable application error code")
    message: str = Field(description="Safe, human-readable error message")
    details: list[ValidationIssue] | None = Field(default=None, description="Field errors for invalid requests")


class ErrorResponse(BaseModel):
    error: ErrorDetail


def error_response(status_code: int, code: str, message: str, details: list[ValidationIssue] | None = None) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=payload.model_dump(exclude_none=True))


def register_error_handlers(app: FastAPI) -> None:
    domain_errors = {
        account_service.AccountNotFoundError: (404, "ACCOUNT_NOT_FOUND", "Account not found"),
        transfer_service.TransferAccountNotFoundError: (404, "ACCOUNT_NOT_FOUND", "Account not found"),
        transfer_service.InvalidTransferAmountError: (422, "INVALID_AMOUNT", "Invalid transfer amount"),
        transfer_service.SameAccountError: (400, "SAME_ACCOUNT_TRANSFER", "Accounts must differ"),
        transfer_service.InsufficientFundsError: (409, "INSUFFICIENT_FUNDS", "Insufficient funds"),
        transfer_service.IdempotencyKeyConflictError: (
            409, "IDEMPOTENCY_KEY_REUSED", "Idempotency key already used for a different transfer"
        ),
    }

    async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
        status_code, code, message = domain_errors[type(exc)]
        return error_response(status_code, code, message)

    for error_type in domain_errors:
        app.add_exception_handler(error_type, handle_domain_error)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            ValidationIssue(location=list(issue["loc"]), message=issue["msg"], type=issue["type"])
            for issue in exc.errors()
        ]
        amount_only = all(issue.location == ["body", "amount"] for issue in issues)
        code = "INVALID_AMOUNT" if amount_only else "VALIDATION_ERROR"
        message = "Invalid transfer amount" if amount_only else "Request validation failed"
        return error_response(422, code, message, issues)

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else "Internal server error"
        return error_response(exc.status_code, code, message)

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_account_transactions_transfer_direction":
            return error_response(409, "ACCOUNT_TRANSACTION_CONFLICT", "Account transaction already exists")
        return error_response(500, "DATABASE_ERROR", "Database operation failed")

    @app.exception_handler(OperationalError)
    async def handle_database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        return error_response(503, "DATABASE_UNAVAILABLE", "Database unavailable")

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return error_response(500, "DATABASE_ERROR", "Database operation failed")

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "INTERNAL_ERROR", "Internal server error")