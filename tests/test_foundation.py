from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import app


def test_openapi_exposes_account_and_transfer_routes() -> None:
    response = TestClient(app).get("/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {
        "/accounts", "/accounts/{account_id}", "/accounts/{account_id}/transactions", "/transfers"
    }


def test_openapi_documents_requests_responses_and_errors() -> None:
    specification = TestClient(app).get("/openapi.json").json()
    paths = specification["paths"]
    schemas = specification["components"]["schemas"]

    account_create = paths["/accounts"]["post"]
    assert "zero-balance" in account_create["summary"]
    assert account_create["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/AccountResponse")
    assert account_create["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert "no input fields" in schemas["AccountCreate"]["description"]

    account_get = paths["/accounts/{account_id}"]["get"]
    assert account_get["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert any(parameter["name"] == "account_id" and parameter["schema"]["exclusiveMinimum"] == 0
               for parameter in account_get["parameters"])

    transfer_create = paths["/transfers"]["post"]
    key = next(parameter for parameter in transfer_create["parameters"] if parameter["name"] == "Idempotency-Key")
    assert key["required"] and "same transfer request" in key["description"]
    assert "retrying" in transfer_create["description"]
    assert transfer_create["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/TransferResponse")
    assert all(transfer_create["responses"][code]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
               for code in ("400", "404", "409", "422"))
    assert "minor units" in schemas["TransferCreate"]["properties"]["amount"]["description"]

    history = paths["/accounts/{account_id}/transactions"]["get"]
    parameters = {parameter["name"]: parameter for parameter in history["parameters"]}
    assert parameters["page"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert set(schemas["AccountTransactionPage"]["properties"]) == {"items", "page", "limit", "total"}
    assert "Debit removes funds" in schemas["AccountTransactionResponse"]["properties"]["direction"]["description"]


def test_database_dependency_provides_session() -> None:
    sessions = get_db()
    try:
        session = next(sessions)
        assert isinstance(session, Session)
        assert session.bind.dialect.name == "postgresql"
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        sessions.close()


def test_unknown_route_returns_consistent_error() -> None:
    response = TestClient(app).get("/unknown")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "NOT_FOUND", "message": "Not Found"}}