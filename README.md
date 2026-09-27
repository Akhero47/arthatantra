# arthatantra
A naive person's implementation of a Money Transfer System 

## Architecture

Requests follow `API -> service -> repository -> PostgreSQL`. The API modules handle HTTP validation,
Pydantic responses, dependency injection, and error translation. Services enforce account and transfer
rules, compute balance changes, and own transaction boundaries and idempotency recovery. Repositories
perform SQLAlchemy reads, ordered PostgreSQL row locks, writes, and database-specific conflict detection;
they never commit a transfer. `models.py` declares tables and constraints, `db.py` provides sessions,
and `config.py` reads the database URL. Alembic migrations in `migrations/` define schema changes.

## Local setup

From the project root, install dependencies in a Python 3.11+ virtual environment:

```sh
python -m pip install -r requirement.txt
cp .env.example .env
docker compose up -d --wait db
python -m alembic upgrade head
```

Run the API with `uvicorn main:app --reload` and open `/docs` on port 8000.
Run `python -m alembic current` to check the migration connection.

Tests require a separate PostgreSQL database. Create it once (skip this command if it already exists),
then migrate it and run Pytest:

```sh
docker compose exec -T db psql -U arthatantra -d postgres -c 'CREATE DATABASE arthatantra_test'
export TEST_DATABASE_URL=postgresql+psycopg://arthatantra:arthatantra@localhost:5432/arthatantra_test
DATABASE_URL="$TEST_DATABASE_URL" python -m alembic upgrade head
python -m pytest -q
```

Pytest refuses to run without `TEST_DATABASE_URL` or if it names the application database.

## Accounts

`POST /accounts` accepts no body (or `{}`) and creates an account with a zero balance.
`GET /accounts/{account_id}` returns its current balance and timestamps, or 404 if it does not exist.
`GET /accounts/{account_id}/transactions` returns debit and credit entries newest first as
`{"items": [...], "page": 1, "limit": 20, "total": 0}`. Use `page` (1 or greater) and
`limit` (1-100) query parameters; the repository uses offset pagination internally. Entries are
ordered by creation time descending, then ID descending for equal timestamps. A missing account
returns 404. This replaces the previous public `offset`/`has_more` pagination contract; clients
using those fields must switch to `page`/`total`.

## Transfers

`POST /transfers` accepts `source_account_id`, `destination_account_id`, and `amount` as JSON integers.
The amount is a positive integer in minor units. A successful transfer returns 201 with its ID, account IDs, amount, and creation time.
Invalid input returns 422, a same-account transfer returns 400, a missing account returns 404, and insufficient funds returns 409.
The service owns the PostgreSQL transaction and locks both accounts before checking the source balance.

## Transfer idempotency

- `POST /transfers` requires an `Idempotency-Key` header containing 1-255 visible ASCII characters without whitespace. Missing or invalid keys return 422. Keys are globally scoped and case-sensitive. Historical P0 transfers have null keys and cannot be replayed by key.
- Request identity is the validated integer tuple `(source_account_id, destination_account_id, amount)` in that order. The service computes `request_hash` as the lowercase hexadecimal SHA-256 digest of the UTF-8 JSON array serialized without spaces (for example, `[1,2,30]`). JSON field order and formatting therefore do not change identity; changing any of the three values does. The key itself is not part of the hash.
- First valid request with a new key: execute the transfer atomically, storing its key and hash on the transfer row in the same database transaction as both balances and ledger entries. Return 201 with the created transfer response.
- Retry with the same key and matching hash: return 200 with the same transfer response fields, including the original ID and creation time. Do not lock or change account balances or create another transfer or ledger entry.
- Same key with a different hash: return 409 for key reuse with a different request. Do not change balances or create records, even if the original source now lacks funds.
- Concurrent requests first check for a committed key, then lock account rows in ID order and recheck before inspecting balances. For requests on disjoint accounts that race to insert the same key, PostgreSQL's UNIQUE constraint picks the winner. The loser rolls back all attempted balance and ledger writes, reads the committed result in a new transaction, and returns 200 or 409 according to the hash. No in-memory or application-level locks are used.
- If the server commits but the response is lost, a retry returns the committed transfer as above. A request that fails validation or whose transaction rolls back leaves no transfer/key record; its key may be retried. Failed business responses are not cached or replayed because only successful transfers have persisted idempotency records.

## API errors

Errors use `{"error": {"code": "...", "message": "..."}}` across the API. Validation errors also include
`details`, a list of field locations, error types, and safe messages; submitted values are never echoed.
Common codes are `VALIDATION_ERROR` and `INVALID_AMOUNT` (422), `ACCOUNT_NOT_FOUND` (404),
`SAME_ACCOUNT_TRANSFER` (400), and `INSUFFICIENT_FUNDS`, `IDEMPOTENCY_KEY_REUSED`, and
`ACCOUNT_TRANSACTION_CONFLICT` (409). Unknown routes use `NOT_FOUND` (404). Unexpected failures
use `INTERNAL_ERROR` or `DATABASE_ERROR` (500); unavailable database connections use
`DATABASE_UNAVAILABLE` (503). Internal SQL and exception details are not returned to clients.

