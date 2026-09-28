# Arthatantra — Money Transfer System

A backend implementation of a money transfer system focused on transactional correctness, concurrency safety, idempotency, and database integrity.

## Overview

Arthatantra is an API-only money transfer system built with FastAPI and PostgreSQL.

The system supports:

* Account creation and retrieval
* Money transfers between accounts
* Transaction history
* Concurrent transfer handling
* Idempotent transfer requests
* PostgreSQL transaction-based atomicity
* Database-level constraints and indexes
* Paginated transaction history
* Automated unit and integration tests
* Dockerized application and PostgreSQL environments
* Alembic database migrations

The implementation prioritizes **correctness and consistency of financial operations over additional features**.

---

# Architecture

Requests follow:

```text
Client
  │
  ▼
FastAPI API Layer
  │
  ▼
Service Layer
  │
  ▼
Repository Layer
  │
  ▼
PostgreSQL
```

### API Layer

The API layer is responsible for:

* HTTP routing
* Request validation
* Pydantic schemas
* Dependency injection
* HTTP status codes
* Translating application errors into API responses
* OpenAPI/Swagger documentation

### Service Layer

The service layer contains business logic and owns transaction boundaries.

It is responsible for:

* Account and transfer business rules
* Balance validation
* Transfer orchestration
* Idempotency behavior
* Transaction coordination
* Maintaining financial invariants

The service layer is intentionally not tightly coupled to FastAPI-specific HTTP exceptions.

### Repository Layer

Repositories handle database access using SQLAlchemy.

They are responsible for:

* Database queries
* Account locking
* Reading and writing transfer records
* Reading and writing transaction-history records
* Database-specific conflict handling

Repositories do not independently commit a transfer transaction. Transaction ownership remains with the service layer.

### Database Layer

PostgreSQL is the source of truth for:

* Account balances
* Transfer records
* Transaction history
* Database constraints
* Idempotency uniqueness
* Concurrency control

SQLAlchemy provides ORM/database access, while Alembic manages schema migrations.

---

# Project Structure

```text
.
├── Dockerfile
├── README.md
├── alembic.ini
├── compose.yaml
├── requirements.txt
│
├── app/
│   ├── account/
│   │   ├── ...
│   │
│   ├── transfer/
│   │   ├── ...
│   │
│   ├── api_errors.py
│   ├── config.py
│   ├── db.py
│   ├── main.py
│   └── models.py
│
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│
├── scripts/
│   └── run-test-app.sh
│
└── tests/
    ├── conftest.py
    ├── test_account_history.py
    ├── test_accounts.py
    ├── test_foundation.py
    ├── test_models.py
    ├── test_transfer_api.py
    ├── test_transfer_request_hash.py
    └── test_transfers.py
```

Python cache directories such as `__pycache__` are generated artifacts and are not part of the application's source code.

---

# Technology Stack

* **Python 3.12**
* **FastAPI** — HTTP API framework
* **Uvicorn** — ASGI server
* **PostgreSQL 16** — relational database
* **SQLAlchemy** — database access/ORM
* **Alembic** — database migrations
* **Pydantic** — request/response validation
* **Pytest** — automated testing
* **Docker** — application containerization
* **Docker Compose** — local application/database orchestration

---

# Database Design

The system uses three primary tables.

## Accounts

Stores the current balance and account metadata.

Conceptually:

```text
accounts
---------
id
balance
created_at
updated_at
```

Balances are stored using integer minor units rather than floating-point values.

For example:

```text
Rs. 100.50
```

is represented as:

```text
10050
```

This avoids floating-point rounding errors in financial calculations.

## Transfers

Represents a completed transfer between two accounts.

Conceptually:

```text
transfers
---------
id
source_account_id
destination_account_id
amount
idempotency_key
request_hash
created_at
```

## Account Transactions

Represents the account-level ledger entries created by a transfer.

A successful transfer produces:

* one DEBIT entry for the source account
* one CREDIT entry for the destination account

This provides an account-centric transaction history.

---

# Financial Invariants

The implementation is designed to preserve the following invariants:

1. Transfer amount must be greater than zero.
2. Source and destination accounts must be different.
3. Both accounts must exist.
4. An account cannot have a negative balance.
5. A transfer must be atomic.
6. A failed transfer must not partially modify balances.
7. Money must be conserved between source and destination accounts.
8. Every successful transfer creates the corresponding transaction-history entries.
9. A successful idempotent request cannot move money twice.
10. Account transaction records represent completed financial operations and are not modified as part of normal transfer processing.

---

# Concurrency and Transaction Safety

Transfers are executed inside a PostgreSQL database transaction.

The transfer flow is conceptually:

```text
BEGIN
  │
  ├── Lock source and destination accounts
  │
  ├── Validate transfer
  │
  ├── Check source balance
  │
  ├── Debit source account
  │
  ├── Credit destination account
  │
  ├── Create transfer record
  │
  ├── Create debit ledger entry
  │
  ├── Create credit ledger entry
  │
  └── COMMIT
```

If any operation fails, the transaction is rolled back.

## Row-level locking

The source and destination account rows are locked using PostgreSQL row-level locking before the source balance is checked.

This prevents concurrent transfers from both observing the same stale balance.

## Deterministic lock ordering

Accounts are locked in ascending account-ID order.

For example:

```text
Transfer A → B
```

and:

```text
Transfer B → A
```

both acquire locks in:

```text
min(A, B) → max(A, B)
```

This reduces the possibility of deadlocks caused by competing transfers acquiring the same locks in different orders.

---

# Idempotency

`POST /transfers` requires an `Idempotency-Key`.

The key:

* contains 1–255 visible ASCII characters
* cannot contain whitespace
* is globally scoped
* is case-sensitive

Historical P0 transfers may have a null idempotency key and cannot be replayed by key.

## Request identity

The request identity is based on:

```text
(source_account_id, destination_account_id, amount)
```

The service computes a SHA-256 request hash from the validated integer tuple.

The key itself is not included in the request hash.

Therefore, changing any of the following changes the request identity:

* source account
* destination account
* amount

## Idempotency behavior

### First request

A new valid idempotency key executes the transfer and stores the key and request hash in the same database transaction as:

* account balance changes
* transfer record
* debit ledger entry
* credit ledger entry

### Same key and same request

The existing transfer is returned.

The transfer is not executed again.

The response is:

```text
200 OK
```

### Same key with a different request

The request is rejected with:

```text
409 Conflict
```

No account balances or records are modified.

### Concurrent identical requests

Concurrent requests using the same idempotency key are protected by the database uniqueness constraint and transaction logic.

PostgreSQL determines the winning insert.

A losing request rolls back any attempted financial changes, retrieves the committed result, and returns the existing transfer when the request hash matches.

No in-memory application locks are used.

### Lost response after successful commit

If the server successfully commits a transfer but the client does not receive the response, retrying the request with the same idempotency key returns the previously committed transfer rather than executing another transfer.

Failed validation and rolled-back business operations do not create an idempotency record, so their keys can be retried.

---

# API Endpoints

## Create Account

```http
POST /accounts
```

Creates an account with an initial balance of zero.

Request body:

```json
{}
```

or no body.

---

## Get Account

```http
GET /accounts/{account_id}
```

Returns:

* account ID
* balance
* creation time
* update time

Returns `404` if the account does not exist.

---

## Transfer Money

```http
POST /transfers
```

Required header:

```http
Idempotency-Key: <unique-key>
```

Request:

```json
{
  "source_account_id": 1,
  "destination_account_id": 2,
  "amount": 1000
}
```

`amount` is an integer representing minor currency units.

For example:

```text
1000 = Rs. 10.00
```

assuming the currency uses two decimal places.

A successful first transfer returns:

```text
201 Created
```

A successful idempotent retry returns:

```text
200 OK
```

---

## Transaction History

```http
GET /accounts/{account_id}/transactions
```

Query parameters:

```text
page
limit
```

Defaults:

```text
page = 1
limit = 20
```

Maximum:

```text
limit = 100
```

Example:

```http
GET /accounts/1/transactions?page=1&limit=20
```

Response structure:

```json
{
  "items": [],
  "page": 1,
  "limit": 20,
  "total": 0
}
```

Transactions are ordered by:

1. creation time descending
2. transaction ID descending for equal timestamps

This provides deterministic ordering.

---

# API Error Handling

Errors use a consistent structure:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable message"
  }
}
```

Validation errors additionally include safe validation details.

Submitted values are not echoed back in validation errors.

Common errors include:

| Error                          | HTTP Status |
| ------------------------------ | ----------: |
| `VALIDATION_ERROR`             |         422 |
| `INVALID_AMOUNT`               |         422 |
| `ACCOUNT_NOT_FOUND`            |         404 |
| `NOT_FOUND`                    |         404 |
| `SAME_ACCOUNT_TRANSFER`        |         400 |
| `INSUFFICIENT_FUNDS`           |         409 |
| `IDEMPOTENCY_KEY_REUSED`       |         409 |
| `ACCOUNT_TRANSACTION_CONFLICT` |         409 |
| `INTERNAL_ERROR`               |         500 |
| `DATABASE_ERROR`               |         500 |
| `DATABASE_UNAVAILABLE`         |         503 |

Internal SQL errors, stack traces, and other implementation details are not returned to API clients.

---

# Database Constraints and Indexes

Database constraints are used as defense in depth in addition to application-level validation.

Important constraints include:

* non-negative account balances
* positive transfer amounts
* valid source and destination foreign keys
* source and destination cannot be the same account
* unique idempotency keys
* valid account transaction directions
* positive transaction amounts

Indexes support important query patterns such as:

* account lookup
* idempotency-key lookup
* account transaction history
* foreign-key lookups

The transaction history query uses an index aligned with account filtering and chronological ordering.

---

# Docker

The project can be run using Docker Compose.

The Compose setup provides:

```text
FastAPI application
        │
        ▼
   PostgreSQL
```

The project also provides a separate PostgreSQL database for automated tests.

```text
Application:
  arthatantra

Tests:
  arthatantra_test
```

This prevents automated tests from modifying the application's database.

## Docker Services

The Compose configuration provides:

* `db` — application PostgreSQL database
* `test-db` — isolated PostgreSQL test database
* `app` — FastAPI application
* `app-test` — optional FastAPI application configured against the test database

The PostgreSQL data is persisted using Docker volumes.

---

# Dockerized Development Workflow

## Start the application database

```bash
docker compose up -d db
```

Check the service:

```bash
docker compose ps
```

## Run application migrations

```bash
docker compose run --rm --no-deps app alembic upgrade head
```

## Start the application

```bash
docker compose up -d app
```

The API is available at:

```text
http://localhost:8000
```

Swagger documentation:

```text
http://localhost:8000/docs
```

## View application logs

```bash
docker compose logs -f app
```

---

# Test Database Workflow

Start the test database:

```bash
docker compose up -d test-db
```

Run migrations against the test database:

```bash
docker compose run --rm \
  --no-deps \
  -e DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@test-db:5432/arthatantra_test \
  app alembic upgrade head
```

Run the test suite:

```bash
docker compose run --rm --no-deps app python -m pytest
```

The test configuration requires `TEST_DATABASE_URL` and refuses to run against the application database.

---

# Running the Application Against the Test Database

A separate `app-test` Compose service is available for manually running the API against the isolated test database.

Start it using:

```bash
docker compose --profile test up -d app-test
```

The test-database API is available at:

```text
http://localhost:8001
```

Swagger:

```text
http://localhost:8001/docs
```

This allows the application to be manually tested without modifying the normal `arthatantra` database.

---

# Convenient Test Application Startup

The project includes:

```text
scripts/run-test-app.sh
```

This script:

1. Starts the test PostgreSQL database.
2. Waits for PostgreSQL to become ready.
3. Runs Alembic migrations against the test database.
4. Starts the FastAPI application configured for the test database.

Run it with:

```bash
./scripts/run-test-app.sh
```

The test application is then available at:

```text
http://localhost:8001/docs
```

---

# Why Migrations Are Explicit

Database migrations are intentionally not executed automatically when the FastAPI application starts.

Automatically running migrations during application startup can be risky in production because:

* multiple application instances may start simultaneously
* application startup becomes dependent on schema migration success
* schema changes can unexpectedly affect application startup
* migration failures can make deployment failures harder to diagnose
* database changes become coupled to application process startup

Instead, Alembic migrations are executed explicitly:

```bash
docker compose run --rm --no-deps app alembic upgrade head
```

This makes schema changes an explicit operational step.

---

# Testing Strategy

The test suite is designed to verify behavior and financial invariants rather than only HTTP responses.

Tests cover:

* account creation
* account retrieval
* account transaction history
* validation
* transfers
* insufficient funds
* same-account transfers
* transaction rollback
* database constraints
* request hashing
* idempotency
* concurrent transfers
* transaction history pagination

Database-specific behavior is tested against PostgreSQL rather than SQLite.

A separate PostgreSQL database is used for tests to prevent test execution from modifying application data.

---

# Concurrency Testing

Concurrency tests use PostgreSQL and concurrent transfer requests.

The important properties being verified include:

* balances never become negative
* concurrent transfers do not lose money
* money remains conserved
* transfers remain atomic
* concurrent idempotent requests do not create duplicate transfers

For example, if an account has enough balance for only a fixed number of concurrent transfers, successful transfers must not exceed the available balance.

---

# Local Development Without Docker

A Python virtual environment can also be used for local development.

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Configure the environment:

```bash
cp .env.example .env
```

Run PostgreSQL using Docker:

```bash
docker compose up -d db
```

Run migrations:

```bash
python -m alembic upgrade head
```

Start FastAPI:

```bash
uvicorn app.main:app --reload
```

The API is available at:

```text
http://localhost:8000
```

Swagger:

```text
http://localhost:8000/docs
```

Check the current migration:

```bash
python -m alembic current
```

---

# Environment Configuration

Database configuration is provided through environment variables.

Example:

```env
DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@localhost:5432/arthatantra
```

For Docker Compose, the application connects to the PostgreSQL service using:

```text
db:5432
```

rather than `localhost`.

Test execution uses a separate:

```env
TEST_DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@test-db:5432/arthatantra_test
```

Real secrets should not be committed to the repository.

Use `.env.example` as the configuration template.

---

# Database Migrations

Alembic is used to manage database schema changes.

Migration files are located in:

```text
migrations/versions/
```

Create a migration:

```bash
python -m alembic revision --autogenerate -m "description"
```

Review the generated migration before applying it.

Apply migrations:

```bash
python -m alembic upgrade head
```

Check the current revision:

```bash
python -m alembic current
```

Downgrade one revision when appropriate:

```bash
python -m alembic downgrade -1
```

---

# Running Tests Locally

Pytest requires a separate PostgreSQL test database.

When using Docker Compose:

```bash
docker compose up -d test-db
```

Run the test database migrations:

```bash
docker compose run --rm \
  --no-deps \
  -e DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@test-db:5432/arthatantra_test \
  app alembic upgrade head
```

Then run:

```bash
docker compose run --rm --no-deps app python -m pytest
```

---

# Design Decisions and Trade-offs

## Integer minor units instead of floating point

Money is represented as integer minor units.

This avoids floating-point precision problems and makes balance arithmetic deterministic.

## PostgreSQL instead of SQLite

PostgreSQL is required because the system relies on database behavior such as:

* row-level locking
* transactions
* unique constraints
* concurrent transaction handling

SQLite would not accurately represent the production database behavior being tested.

## Service-owned transactions

The service layer owns the transaction boundary because a transfer consists of multiple database operations that must succeed or fail together.

Repositories therefore do not independently commit transfers.

## Row-level locking

Account rows are explicitly locked before the balance check.

This allows concurrent requests to safely operate on shared account balances.

## Deterministic lock ordering

Accounts are locked in ID order to reduce deadlock risk when transfers occur in opposite directions.

## Database-enforced idempotency

Idempotency is backed by a PostgreSQL uniqueness constraint rather than an in-memory data structure.

This ensures correctness even when multiple application instances are running.

## Explicit migrations

Migrations are executed as a separate operational step rather than automatically during application startup.

This keeps application startup independent from schema modification.

## Separate test database

Tests use a dedicated PostgreSQL database to prevent test execution from modifying application data and to ensure database-specific behavior is tested against PostgreSQL.

---

# Priorities and Scope

The implementation prioritizes:

1. Financial correctness
2. Transaction atomicity
3. Concurrency safety
4. Idempotency
5. Database integrity
6. Testability
7. Maintainability
8. Containerized development

Additional infrastructure such as Redis, Kafka, Celery, Kubernetes, microservices, or event sourcing is intentionally not included because it is not required for the scope of this system.

---

# Future Improvements

Possible future improvements include:

* Authentication and authorization
* Account funding/deposit APIs
* Transfer cancellation or reversal workflows
* Rate limiting
* Structured application logging
* CI/CD pipeline
* Production secret management
* Metrics and monitoring
* More extensive API contract testing
* Production deployment configuration
* Stronger financial audit/reporting capabilities

These are outside the current assignment scope.

---

# Running the Project — Quick Reference

### Normal application

```bash
docker compose up -d db
docker compose run --rm --no-deps app alembic upgrade head
docker compose up -d app
```

API:

```text
http://localhost:8000/docs
```

### Test suite

```bash
docker compose up -d test-db

docker compose run --rm \
  --no-deps \
  -e DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@test-db:5432/arthatantra_test \
  app alembic upgrade head

docker compose run --rm --no-deps app python -m pytest
```

### Run API against test database

```bash
./scripts/run-test-app.sh
```

Test API:

```text
http://localhost:8001/docs
```

### Stop everything

```bash
docker compose down
```

### Remove database volumes

```bash
docker compose down -v
```

> Removing volumes permanently deletes the local PostgreSQL data.
