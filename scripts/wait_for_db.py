"""Wait until the configured PostgreSQL database accepts connections."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.db import ensure_database


def main() -> int:
    url = make_url(settings.database_url)
    print(f"waiting for postgres at {url.host}:{url.port or 5432}/{url.database} ...", flush=True)
    for attempt in range(1, 91):
        try:
            engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            print("postgres is ready", flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001 - retry on any connection error
            if attempt % 5 == 0:
                print(f"  attempt {attempt}: {type(exc).__name__}: {exc}", flush=True)
            # the database itself may not exist yet (fresh container)
            try:
                ensure_database(settings.database_url, settings.admin_database_url)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
    print("postgres did not become ready in time", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
