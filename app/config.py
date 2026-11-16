import os
from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def _database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev"
    )


def _derive(url: str, database: str) -> str:
    """Same server/credentials as ``url`` but a different database name."""
    parsed = make_url(url)
    return parsed.set(database=database).render_as_string(hide_password=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CROSSBORDER_", env_file=".env", extra="ignore")

    database_url: str = _database_url()
    # Dedicated database used by the test-suite; created automatically if absent.
    test_database_url: str = os.environ.get(
        "TEST_DATABASE_URL", _derive(_database_url(), "ccdev_test")
    )
    # Used only to CREATE DATABASE when the target database does not exist.
    admin_database_url: str = os.environ.get(
        "ADMIN_DATABASE_URL", _derive(_database_url(), "postgres")
    )
    # Tolerance for money comparisons after decimal rounding.
    amount_scale: int = 2
    auto_create_schema: bool = True


settings = Settings()


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-settings.amount_scale))
