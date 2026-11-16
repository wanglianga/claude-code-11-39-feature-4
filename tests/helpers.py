"""Scenario helpers shared by the acceptance tests."""

from __future__ import annotations

from datetime import datetime, timezone

DECL = "DECL-2026-100"
INV = "INV-2026-001"

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
EXPORT_DATE = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
VALUE_DATE = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


LINES = [
    {"line_no": 1, "sku": "SKU-AA-100", "quantity": "100", "amount": "1200.00", "description": "Widget A"},
    {"line_no": 2, "sku": "SKU-BB-200", "quantity": "50", "amount": "1800.00", "description": "Widget B"},
    {"line_no": 3, "sku": "SKU-CC-300", "quantity": "20", "amount": "2000.00", "description": "Widget C"},
]


def post(client, headers, path, body):
    r = client.post(path, json=body, headers=headers)
    assert r.status_code < 300, f"{path} -> {r.status_code}: {r.text}"
    return r.json()


def seed_contract(client, headers, entity="ENT-A"):
    return post(
        client,
        headers,
        "/api/v1/intake/documents",
        {
            "doc_type": "contract",
            "entity_code": entity,
            "external_no": "C-2026-001",
            "original_location": "s3://contracts/C-2026-001.pdf",
            "event_time": T0.isoformat(),
            "received_time": T0.isoformat(),
            "currency": "USD",
            "amount": "5000.00",
            "lines": [{"contract_no": "C-2026-001", "buyer_name": "Global Buyer LLC", "currency": "USD", "amount": "5000.00"}],
        },
    )


def seed_invoice(client, headers, entity="ENT-A", invoice_no=INV, declaration_no=DECL, lines=None):
    lines = lines or LINES
    return post(
        client,
        headers,
        "/api/v1/intake/documents",
        {
            "doc_type": "invoice",
            "entity_code": entity,
            "external_no": invoice_no,
            "original_location": f"s3://invoices/{invoice_no}.pdf",
            "event_time": T0.isoformat(),
            "received_time": T0.isoformat(),
            "currency": "USD",
            "amount": sum(float(l["amount"]) for l in lines),
            "lines": [
                {
                    "line_no": l["line_no"],
                    "sku": l["sku"],
                    "description": l["description"],
                    "quantity": l["quantity"],
                    "amount": l["amount"],
                    "currency": "USD",
                    "invoice_no": invoice_no,
                    "contract_no": "C-2026-001",
                    "declaration_no": declaration_no,
                    "export_date": EXPORT_DATE.isoformat(),
                }
                for l in lines
            ],
        },
    )


def seed_customs(client, headers, entity="ENT-A", declaration_no=DECL, lines=None, doc_type="customs_archive"):
    lines = lines or LINES
    return post(
        client,
        headers,
        "/api/v1/intake/documents",
        {
            "doc_type": doc_type,
            "entity_code": entity,
            "external_no": declaration_no if doc_type == "customs_archive" else f"{declaration_no}-REV1",
            "original_location": f"s3://customs/{declaration_no}-{doc_type}.xml",
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
                    "hs_code": l.get("hs_code", "8471.30"),
                    "declaration_no": declaration_no,
                    "revises_line_no": l.get("revises_line_no"),
                }
                for l in lines
            ],
        },
    )


def seed_tax(client, headers, entity="ENT-A", batch_no="B-001", declaration_no=DECL, issue_time=None, sequence_no=None,
             review_status="approved"):
    return post(
        client,
        headers,
        "/api/v1/intake/documents",
        {
            "doc_type": "tax_receipt",
            "entity_code": entity,
            "external_no": batch_no,
            "original_location": f"s3://tax/{batch_no}.json",
            "event_time": (issue_time or T0).isoformat(),
            "received_time": T0.isoformat(),
            "lines": [
                {
                    "line_no": l["line_no"],
                    "batch_no": batch_no,
                    "declaration_no": declaration_no,
                    "review_status": review_status,
                    "quantity": l["quantity"],
                    "amount": l["amount"],
                    "currency": "USD",
                    "issue_time": (issue_time or T0).isoformat(),
                    "sequence_no": sequence_no,
                }
                for l in LINES
            ],
        },
    )


def seed_bank(client, headers, entity="ENT-A", txn_ref="TXN-001", amount="5000.00", remittance_info=None,
              value_date=None):
    return post(
        client,
        headers,
        "/api/v1/intake/documents",
        {
            "doc_type": "bank_statement",
            "entity_code": entity,
            "external_no": f"STMT-{txn_ref}",
            "original_location": f"s3://bank/{txn_ref}.csv",
            "event_time": (value_date or VALUE_DATE).isoformat(),
            "received_time": (value_date or VALUE_DATE).isoformat(),
            "currency": "USD",
            "amount": amount,
            "lines": [
                {
                    "txn_ref": txn_ref,
                    "payer_name": "Global Buyer LLC",
                    "currency": "USD",
                    "amount": amount,
                    "value_date": (value_date or VALUE_DATE).isoformat(),
                    "remittance_info": remittance_info,
                }
            ],
        },
    )


def export_line_ids(client, headers, entity="ENT-A"):
    post(client, headers, f"/api/v1/entities/{entity}/resolve", {})
    rows = client.get(f"/api/v1/entities/{entity}/dossiers", headers=headers).json()
    return sorted(r["export_line_id"] for r in rows if r["export_line_id"])


def submit_all(client, headers, export_ids, events=("submitted", "verified")):
    for eid in export_ids:
        for event in events:
            post(
                client,
                headers,
                f"/api/v1/export-lines/{eid}/declaration-events",
                {"event": event, "reference": f"REF-{eid}-{event}"},
            )
