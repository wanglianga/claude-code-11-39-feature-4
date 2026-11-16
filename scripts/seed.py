"""Seed demo data: entities, users/API keys, contracts, invoices, customs, tax receipts, bank flows.

Idempotent: re-running does nothing unless --reset is given.
Prints the API keys so the README instructions can be followed verbatim.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text

from app import models as m
from app.config import settings
from app.db import SessionLocal, ensure_database, init_db
from app.schemas import CoverageProofRequest, IntakeLine, IntakeRequest
from app.services import balance, facts, ledgers, resolver
from app.services.intake import ingest_document
from app.services.timeliness import build_run
from app.util import key_hash

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
EXPORT_DATE = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
VALUE_DATE = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
ANALYSIS_CUTOFF = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
BANK_ACCOUNT_USD = "ACCT-ENT-A-USD"
DECL = "DECL-2026-100"
INV = "INV-2026-001"

USERS = [
    ("admin", "admin", "demo-admin-key", None),
    ("analyst1", "analyst", "demo-analyst-1-key", ["ENT-A"]),
    ("analyst2", "analyst", "demo-analyst-2-key", ["ENT-A"]),
    ("entity-b-analyst", "analyst", "demo-analyst-b-key", ["ENT-B"]),
    ("viewer", "viewer", "demo-viewer-key", ["ENT-A", "ENT-B"]),
]

LINES = [
    dict(line_no=1, sku="SKU-AA-100", description="Widget A", quantity=Decimal("100"), amount=Decimal("1200.00")),
    dict(line_no=2, sku="SKU-BB-200", description="Widget B", quantity=Decimal("50"), amount=Decimal("1800.00")),
    dict(line_no=3, sku="SKU-CC-300", description="Widget C", quantity=Decimal("20"), amount=Decimal("2000.00")),
]


def _req(**kw) -> IntakeRequest:
    base = dict(original_location="seed://placeholder", event_time=T0, received_time=T0)
    base.update(kw)
    return IntakeRequest(**base)


def seed(reset: bool = False) -> None:
    ensure_database(settings.database_url, settings.admin_database_url)
    init_db()

    db = SessionLocal()
    try:
        if reset:
            for table in reversed(m.Base.metadata.sorted_tables):
                db.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
            db.commit()

        if db.scalar(select(m.Entity.id).limit(1)) is not None:
            print("database already seeded — use --reset to rebuild")
            return

        ents = {
            "ENT-A": m.Entity(code="ENT-A", name="辰星跨境贸易有限公司", country="CN"),
            "ENT-B": m.Entity(code="ENT-B", name="海岳国际供应链有限公司", country="CN"),
        }
        db.add_all(ents.values())
        db.flush()

        for name, role, key, codes in USERS:
            user = m.User(name=name, role=role, api_key_hash=key_hash(key))
            db.add(user)
            db.flush()
            for code in codes or []:
                db.add(m.UserEntityGrant(user_id=user.id, entity_id=ents[code].id))
        db.flush()

        actor = "seed"

        # contract
        ingest_document(
            db,
            _req(
                doc_type="contract",
                entity_code="ENT-A",
                external_no="C-2026-001",
                original_location="s3://demo/contracts/C-2026-001.pdf",
                currency="USD",
                amount=Decimal("5000.00"),
                lines=[IntakeLine(contract_no="C-2026-001", buyer_name="Global Buyer LLC", currency="USD",
                                  amount=Decimal("5000.00"))],
            ),
            actor,
        )

        # invoice (3 lines) — each line becomes an export line
        ingest_document(
            db,
            _req(
                doc_type="invoice",
                entity_code="ENT-A",
                external_no=INV,
                original_location=f"s3://demo/invoices/{INV}.pdf",
                currency="USD",
                amount=Decimal("5000.00"),
                lines=[
                    IntakeLine(
                        line_no=l["line_no"],
                        sku=l["sku"],
                        description=l["description"],
                        quantity=l["quantity"],
                        amount=l["amount"],
                        currency="USD",
                        invoice_no=INV,
                        contract_no="C-2026-001",
                        declaration_no=DECL,
                        export_date=EXPORT_DATE,
                    )
                    for l in LINES
                ],
            ),
            actor,
        )

        # customs archive (original basis)
        ingest_document(
            db,
            _req(
                doc_type="customs_archive",
                entity_code="ENT-A",
                external_no=DECL,
                original_location=f"s3://demo/customs/{DECL}.xml",
                lines=[
                    IntakeLine(
                        line_no=l["line_no"],
                        sku=l["sku"],
                        description=l["description"],
                        quantity=l["quantity"],
                        amount=l["amount"],
                        currency="USD",
                        hs_code="8471.30",
                        declaration_no=DECL,
                    )
                    for l in LINES
                ],
            ),
            actor,
        )

        # tax receipt (batch 1)
        ingest_document(
            db,
            _req(
                doc_type="tax_receipt",
                entity_code="ENT-A",
                external_no="B-2026-09-01",
                original_location="s3://demo/tax/B-2026-09-01.json",
                lines=[
                    IntakeLine(
                        line_no=l["line_no"],
                        batch_no="B-2026-09-01",
                        declaration_no=DECL,
                        review_status="approved",
                        quantity=l["quantity"],
                        amount=l["amount"],
                        currency="USD",
                        issue_time=T0,
                        sequence_no=1,
                    )
                    for l in LINES
                ],
            ),
            actor,
        )
        db.commit()

        # declaration states: submitted + verified for all three lines
        exports = db.scalars(
            select(m.ExportLine).where(m.ExportLine.entity_id == ents["ENT-A"].id).order_by(m.ExportLine.id)
        ).all()
        for e in exports:
            ledgers.apply_declaration_event(db, e, "submitted", actor=actor, reference="DECL-SUBMIT-2026-09")
            ledgers.apply_declaration_event(db, e, "verified", actor=actor, reference="TAX-REVIEW-2026-09")
        db.commit()

        # bank flows:
        #  1) unique SKU reference, partial (1000 against 1200)
        #  2) combined remittance with no line key (2500 for lines 2+3 = 3800) -> candidates only
        ingest_document(
            db,
            _req(
                doc_type="bank_statement",
                entity_code="ENT-A",
                external_no="STMT-2026-10-A",
                original_location="s3://demo/bank/2026-10-a.csv",
                event_time=VALUE_DATE,
                received_time=VALUE_DATE,
                currency="USD",
                amount=Decimal("1000.00"),
                lines=[
                    IntakeLine(
                        txn_ref="TXN-2026-10-A",
                        payer_name="Global Buyer LLC",
                        currency="USD",
                        amount=Decimal("1000.00"),
                        value_date=VALUE_DATE,
                        remittance_info="ORDER SETTLEMENT SKU-AA-100",
                    )
                ],
            ),
            actor,
        )
        ingest_document(
            db,
            _req(
                doc_type="bank_statement",
                entity_code="ENT-A",
                external_no="STMT-2026-10-B",
                original_location="s3://demo/bank/2026-10-b.csv",
                event_time=VALUE_DATE,
                received_time=VALUE_DATE,
                currency="USD",
                amount=Decimal("2500.00"),
                lines=[
                    IntakeLine(
                        txn_ref="TXN-2026-10-B",
                        payer_name="Global Buyer LLC",
                        currency="USD",
                        amount=Decimal("2500.00"),
                        value_date=VALUE_DATE,
                        remittance_info="OCTOBER CONSOLIDATED PAYMENT",
                    )
                ],
            ),
            actor,
        )
        db.commit()

        resolver.resolve_all(db, ents["ENT-A"].id)
        db.commit()
        balance.run_reconciliation_pass(db, entity_id=ents["ENT-A"].id, actor="seed")
        db.commit()
        run = facts.compile_facts(db, entity_id=ents["ENT-A"].id, actor="seed")
        db.commit()

        # ---- timeliness side-car: bank coverage calendar + one demo analysis run ----
        # Coverage proofs (one extraction batch per month) span the whole post-approval window
        # so every seeded export line gets a verifiable observation boundary.
        from app.services.timeliness import coverage as coverage_service

        coverage_dates = [
            # coverage on the approval day is allowed; the strict "boundary after approval"
            # rule is enforced separately (horizon <= approved_at is treated as uncovered)
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 31, tzinfo=timezone.utc),
            datetime(2026, 11, 30, tzinfo=timezone.utc),
            datetime(2026, 12, 31, tzinfo=timezone.utc),
        ]
        for idx, day in enumerate(coverage_dates, start=1):
            coverage_service.register_coverage_proof(
                db,
                CoverageProofRequest(
                    entity_code="ENT-A",
                    bank_account=BANK_ACCOUNT_USD,
                    currency="USD",
                    coverage_date=day,
                    status="covered",
                    completion=Decimal("1.0000"),
                    extraction_batch_no=f"EX-2026-{idx:02d}",
                    issued_at=day,
                    received_at=day,
                    sequence_no=idx,
                    note="seeded monthly bank extraction coverage proof",
                ),
                ents["ENT-A"],
            )
        db.commit()
        timeliness_run = build_run(
            db,
            entity_id=ents["ENT-A"].id,
            actor="seed",
            cutoff=ANALYSIS_CUTOFF,
            group_filter={"review_batch_nos": [], "currencies": [], "export_line_ids": []},
            mature_horizon_days=30,
        )
        db.commit()

        print("seeded: entities=%d users=%d export_lines=%d facts=%d unallocated=%d differences=%d"
              % (len(ents), len(USERS), len(exports), run.facts_count, run.unallocated_count, run.differences_count))
        print("timeliness: run_id=%d sample_counts=%s tier_counts=%s"
              % (
                  timeliness_run.id,
                  timeliness_run.result.get("sample_counts"),
                  timeliness_run.result.get("tier_counts"),
              ))
        print()
        print("API keys (X-API-Key):")
        for name, role, key, codes in USERS:
            print(f"  {name:<18} role={role:<8} key={key:<24} entities={','.join(codes or ['*'])}")
        print()
        print("start: uvicorn app.main:app --host 0.0.0.0 --port 8000")
        print("swagger: http://127.0.0.1:8000/docs")
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--if-empty", action="store_true")
    args = ap.parse_args()
    if args.if_empty:
        db = SessionLocal()
        try:
            if db.scalar(select(m.Entity.id).limit(1)) is not None:
                print("seed skipped: data present")
                return 0
        finally:
            db.close()
    seed(reset=args.reset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
