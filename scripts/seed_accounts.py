from secrets import randbelow

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from app.config import settings
from app.db import engine
from app.models import Account


FIRST_ACCOUNT_ID = 1
LAST_ACCOUNT_ID = 100
MAX_BALANCE_MINOR_UNITS = 100_000


def seed_accounts() -> int:
    database_url = make_url(settings.database_url)
    if database_url.get_backend_name() != "postgresql" or not (database_url.database or "").endswith("_test"):
        raise RuntimeError("Refusing to seed unless DATABASE_URL targets a PostgreSQL database ending in _test")

    account_rows = [
        {"id": account_id, "balance": randbelow(MAX_BALANCE_MINOR_UNITS + 1)}
        for account_id in range(FIRST_ACCOUNT_ID, LAST_ACCOUNT_ID + 1)
    ]

    with engine.begin() as connection:
        result = connection.execute(
            insert(Account).values(account_rows).on_conflict_do_nothing(index_elements=[Account.id])
        )
        connection.execute(
            text(
                "SELECT setval("
                "pg_get_serial_sequence('accounts', 'id'), "
                "GREATEST(COALESCE((SELECT MAX(id) FROM accounts), 1), :last_id), "
                "true)"
            ),
            {"last_id": LAST_ACCOUNT_ID},
        )

    return result.rowcount


if __name__ == "__main__":
    inserted = seed_accounts()
    print(
        f"Inserted {inserted} accounts; preserved existing accounts in IDs "
        f"{FIRST_ACCOUNT_ID}-{LAST_ACCOUNT_ID}. Balances are random minor units "
        f"from 0 to {MAX_BALANCE_MINOR_UNITS}."
    )