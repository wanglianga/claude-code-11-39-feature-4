import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    from app.db import ensure_database

    ensure_database(settings.test_database_url, settings.admin_database_url)
    eng = create_engine(settings.test_database_url, future=True, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    import app.models  # noqa: F401  (registers tables)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
    # leave no tables behind, so a live server on the test DB cannot see stale rows
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


ADMIN_KEY = "adminkey-0001"
ANALYST_KEY = "analystkey-0001"
SECOND_ANALYST_KEY = "analystkey-0002"
OUTSIDER_KEY = "outsiderkey-0001"


@pytest.fixture()
def bootstrap(client, db):
    """Create entities, users, contracts and invoice/customs declarations."""
    h = {"X-API-Key": ADMIN_KEY}

    def mkdump_users():
        from app import models as m
        from app.util import key_hash

        db.add(m.User(name="admin", role="admin", api_key_hash=key_hash(ADMIN_KEY)))
        db.commit()

    mkdump_users()

    for code, name in (("ENT-A", "Alpha Trading"), ("ENT-B", "Beta Trading")):
        r = client.post("/api/v1/admin/entities", json={"code": code, "name": name}, headers=h)
        assert r.status_code == 201, r.text
    for name, key, role, codes in (
        ("analyst1", ANALYST_KEY, "analyst", ["ENT-A"]),
        ("analyst2", SECOND_ANALYST_KEY, "analyst", ["ENT-A"]),
        ("outsider", OUTSIDER_KEY, "analyst", ["ENT-B"]),
    ):
        r = client.post(
            "/api/v1/admin/users",
            json={"name": name, "role": role, "api_key": key, "entity_codes": codes},
            headers=h,
        )
        assert r.status_code == 201, r.text
    return {"admin": h, "analyst1": {"X-API-Key": ANALYST_KEY}, "analyst2": {"X-API-Key": SECOND_ANALYST_KEY},
            "outsider": {"X-API-Key": OUTSIDER_KEY}}
