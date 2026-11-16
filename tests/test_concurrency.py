"""True-concurrency test: real uvicorn server, parallel HTTP confirmations, separate transactions.

The sequential test in test_acceptance.py exercises the same rules, but only a real
multi-transaction race can prove the row-lock based balance controller is atomic.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from helpers import DECL, INV, LINES, T0, VALUE_DATE

ROOT = Path(__file__).resolve().parent.parent
ADMIN_KEY = "concurrency-admin"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    from app.config import settings
    from app.db import Base, ensure_database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    ensure_database(settings.test_database_url, settings.admin_database_url)
    eng = create_engine(settings.test_database_url, future=True)
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)

    # bootstrap admin directly in the DB
    import app.models as m
    from app.util import key_hash

    Session = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    s = Session()
    s.add(m.User(name="conc-admin", role="admin", api_key_hash=key_hash(ADMIN_KEY)))
    s.commit()
    s.close()

    port = _free_port()
    env = os.environ.copy()
    env["DATABASE_URL"] = settings.test_database_url
    env["CROSSBORDER_AUTO_CREATE_SCHEMA"] = "true"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.terminate()
        out = proc.stdout.read().decode() if proc.stdout else ""
        pytest.fail(f"server did not start: {out[-3000:]}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _setup(client: httpx.Client, base: str):
    h = {"X-API-Key": ADMIN_KEY}
    for code, name in (("ENT-A", "Alpha Trading"), ("ENT-B", "Beta Trading")):
        r = client.post(base + "/api/v1/admin/entities", json={"code": code, "name": name}, headers=h)
        assert r.status_code == 201, r.text
    for name, key, role, codes in (
        ("analyst1", "conc-a1", "analyst", ["ENT-A"]),
        ("analyst2", "conc-a2", "analyst", ["ENT-A"]),
    ):
        r = client.post(
            base + "/api/v1/admin/users",
            json={"name": name, "role": role, "api_key": key, "entity_codes": codes},
            headers=h,
        )
        assert r.status_code == 201, r.text

    def post(path, body, key=ADMIN_KEY):
        r = client.post(base + path, json=body, headers={"X-API-Key": key})
        assert r.status_code < 300, r.text
        return r.json()

    post(
        "/api/v1/intake/documents",
        {
            "doc_type": "contract",
            "entity_code": "ENT-A",
            "external_no": "C-2026-001",
            "original_location": "s3://contracts/C.pdf",
            "event_time": T0.isoformat(),
            "received_time": T0.isoformat(),
            "currency": "USD",
            "amount": "5000.00",
            "lines": [{"contract_no": "C-2026-001", "currency": "USD", "amount": "5000.00"}],
        },
    )
    post(
        "/api/v1/intake/documents",
        {
            "doc_type": "invoice",
            "entity_code": "ENT-A",
            "external_no": INV,
            "original_location": "s3://invoices/INV-2026-001.pdf",
            "event_time": T0.isoformat(),
            "received_time": T0.isoformat(),
            "currency": "USD",
            "amount": "5000.00",
            "lines": [
                {
                    "line_no": l["line_no"],
                    "sku": l["sku"],
                    "description": l["description"],
                    "quantity": l["quantity"],
                    "amount": l["amount"],
                    "currency": "USD",
                    "invoice_no": INV,
                    "contract_no": "C-2026-001",
                    "declaration_no": DECL,
                    "export_date": datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc).isoformat(),
                }
                for l in LINES
            ],
        },
    )
    post(
        "/api/v1/intake/documents",
        {
            "doc_type": "customs_archive",
            "entity_code": "ENT-A",
            "external_no": DECL,
            "original_location": "s3://customs/dec.xml",
            "event_time": T0.isoformat(),
            "received_time": T0.isoformat(),
            "lines": [
                {
                    "line_no": l["line_no"],
                    "sku": l["sku"],
                    "description": l["description"],
                    "quantity": l["quantity"],
                    "amount": l["amount"],
                    "currency": "USD",
                    "declaration_no": DECL,
                }
                for l in LINES
            ],
        },
    )
    post(
        "/api/v1/intake/documents",
        {
            "doc_type": "tax_receipt",
            "entity_code": "ENT-A",
            "external_no": "B-001",
            "original_location": "s3://tax/B-001.json",
            "event_time": T0.isoformat(),
            "received_time": T0.isoformat(),
            "lines": [
                {
                    "line_no": l["line_no"],
                    "batch_no": "B-001",
                    "declaration_no": DECL,
                    "review_status": "approved",
                    "quantity": l["quantity"],
                    "amount": l["amount"],
                    "currency": "USD",
                    "issue_time": T0.isoformat(),
                }
                for l in LINES
            ],
        },
    )
    post("/api/v1/entities/ENT-A/resolve", {}, key="conc-a1")
    dossiers = client.get(base + "/api/v1/entities/ENT-A/dossiers", headers={"X-API-Key": "conc-a1"}).json()
    ids = sorted(d["export_line_id"] for d in dossiers if d["export_line_id"])
    for eid in ids:
        for event in ("submitted", "verified"):
            post(f"/api/v1/export-lines/{eid}/declaration-events", {"event": event}, key="conc-a1")

    # combined remittance (no unique reference) covering only 1 of the 3 lines
    post(
        "/api/v1/intake/documents",
        {
            "doc_type": "bank_statement",
            "entity_code": "ENT-A",
            "external_no": "STMT-CONC",
            "original_location": "s3://bank/conc.csv",
            "event_time": VALUE_DATE.isoformat(),
            "received_time": VALUE_DATE.isoformat(),
            "currency": "USD",
            "amount": "1000.00",
            "lines": [
                {
                    "txn_ref": "TXN-CONC",
                    "currency": "USD",
                    "amount": "1000.00",
                    "value_date": VALUE_DATE.isoformat(),
                    "remittance_info": "MONTHLY BATCH, NO LINE REFERENCE",
                }
            ],
        },
    )
    post("/api/v1/entities/ENT-A/reconcile-pass", {}, key="conc-a1")
    cands = client.get(
        base + "/api/v1/entities/ENT-A/candidates?status=proposed", headers={"X-API-Key": "conc-a1"}
    ).json()
    assert len(cands) == 3
    return ids, cands


def test_parallel_confirmations_respect_both_balances(server):
    base = server
    with httpx.Client(timeout=30, trust_env=False) as client:
        ids, cands = _setup(client, base)

        def confirm(pair):
            idx, cand = pair
            key = "conc-a1" if idx % 2 == 0 else "conc-a2"
            return httpx.post(
                f"{base}/api/v1/candidates/{cand['id']}/confirm",
                json={"amount": "600.00"},
                headers={"X-API-Key": key},
                timeout=30,
                trust_env=False,
            ).status_code

        with ThreadPoolExecutor(max_workers=3) as pool:
            codes = list(pool.map(confirm, list(enumerate(cands))))

        assert 201 in codes, codes
        # 1000.00 available: at most one 600.00 confirmation can succeed
        assert codes.count(201) == 1, f"atomic controller must allow exactly one: {codes}"
        assert codes.count(409) == 2, codes

        balances = client.get(
            base + "/api/v1/entities/ENT-A/balances", headers={"X-API-Key": "conc-a1"}
        ).json()
        bank = [b for b in balances["bank_transactions"] if b["txn_ref"] == "TXN-CONC"][0]
        committed = Decimal(bank["committed"])
        assert committed == Decimal("600.00")
        assert committed + Decimal(bank["available"]) == Decimal(bank["total"])
        for row in balances["export_lines"]:
            assert Decimal(row["committed"]) <= Decimal(row["total"])
            assert Decimal(row["committed"]) + Decimal(row["available"]) == Decimal(row["total"])

        cases = client.get(
            base + "/api/v1/entities/ENT-A/difference-cases", headers={"X-API-Key": "conc-a1"}
        ).json()
        assert any(c["kind"] == "over_allocation_attempt" for c in cases)

        # the remaining 400.00 must stay unallocated (never auto-derived)
        run = client.post(
            base + "/api/v1/facts/compile", json={"entity_code": "ENT-A"}, headers={"X-API-Key": "conc-a1"}
        ).json()
        assert len(run["result"]["facts"]) == 1
        assert Decimal(run["result"]["facts"][0]["amount"]) == Decimal("600.00")
        unalloc = [u for u in run["result"]["unallocated"] if u["kind"] == "unallocated_bank_amount"]
        assert Decimal(unalloc[0]["unallocated_amount"]) == Decimal("400.00")
