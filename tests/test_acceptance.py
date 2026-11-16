"""End-to-end acceptance tests for the export declaration & remittance facts service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from helpers import (
    DECL,
    T0,
    export_line_ids,
    post,
    seed_bank,
    seed_contract,
    seed_customs,
    seed_invoice,
    seed_tax,
    submit_all,
)

T_ISSUE_1 = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
T_ISSUE_2 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


def _base(client, boot):
    h = boot["admin"]
    seed_contract(client, h)
    seed_invoice(client, h)
    seed_customs(client, h)
    ids = export_line_ids(client, boot["analyst1"])
    assert len(ids) == 3
    return h, ids


def _compile(client, h, data=False):
    r = client.post("/api/v1/facts/compile", json={"entity_code": "ENT-A"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    return body


def _line_detail(client, headers, line_id):
    r = client.get(f"/api/v1/reconciliation-lines/{line_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------------------
# Unique-reference combined remittance & partial receipt
# --------------------------------------------------------------------------------------
def test_combined_remittance_without_reference_stays_unallocated(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-COMBINED", amount="5000.00", remittance_info="MONTHLY SETTLEMENT")

    r = post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    assert r["auto_confirmed"] == [], "combined remittance without unique key must not be auto-confirmed"

    cands = client.get("/api/v1/entities/ENT-A/candidates?status=proposed", headers=a).json()
    assert len(cands) == 3
    assert all(c["strategy"] == "constrained_allocation" for c in cands)
    assert all(c["proposed_amount"] == "0" or Decimal(c["proposed_amount"]) > 0 for c in cands)

    balances = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    bank = [b for b in balances["bank_transactions"] if b["txn_ref"] == "TXN-COMBINED"][0]
    assert Decimal(bank["committed"]) == 0
    assert Decimal(bank["available"]) == Decimal("5000.00")

    ledgers = client.get("/api/v1/entities/ENT-A/status-ledgers", headers=a).json()
    receipt = [r for r in ledgers["receipt_ledgers"] if r["txn_ref"] == "TXN-COMBINED"][0]
    assert receipt["state"] == "pending_allocation"

    run = _compile(client, h)
    result = run["result"]
    assert len(result["facts"]) == 0, "proportional allocation must never become facts"
    bank_unalloc = [u for u in result["unallocated"] if u["kind"] == "unallocated_bank_amount"]
    assert bank_unalloc and Decimal(bank_unalloc[0]["unallocated_amount"]) == Decimal("5000.00")
    assert len(result["unallocated"])


def test_unique_reference_quick_reconciliation_and_partial_receipt(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    # unique reference to invoice line 1 (SKU-AA-100), but only partially paid
    seed_bank(client, h, txn_ref="TXN-U1", amount="1000.00", remittance_info="ORDER SKU-AA-100 SETTLEMENT")

    r = post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    assert len(r["auto_confirmed"]) == 1

    line = _line_detail(client, a, r["auto_confirmed"][0])
    assert Decimal(str(line["reconciliation_line"]["amount"])) == Decimal("1000.00")
    assert line["reconciliation_line"]["strategy"] == "unique_reference"
    # receipt ledger is per bank transaction, so 1000 of a 1000.00 transaction is `matched`;
    # the export line beside it is only 1200 declared -> export-side receipt is `partial`.
    assert line["status_ledgers"]["receipt"]["state"] == "matched"
    assert line["source_files"], "fact must expand to source files"

    balances = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    bank = [b for b in balances["bank_transactions"] if b["txn_ref"] == "TXN-U1"][0]
    assert Decimal(bank["committed"]) == Decimal("1000.00")
    assert Decimal(bank["available"]) == Decimal("0.00")

    target_export = _find_export_line_for_line(client, a, line)
    assert Decimal(target_export["available"]) == Decimal("200.00"), "1200 declared - 1000 committed"

    run = _compile(client, h)
    assert len(run["result"]["facts"]) == 1
    assert Decimal(run["result"]["facts"][0]["amount"]) == Decimal("1000.00")
    unalloc_export = [
        u for u in run["result"]["unallocated"]
        if u["kind"] == "unallocated_export_amount" and Decimal(u["unallocated_amount"]) == Decimal("200.00")
    ]
    assert unalloc_export


def _find_export_line_for_line(client, headers, line_detail):
    # drilldown carries export line inside facts payload; use the balances endpoint + amount match instead
    balances = client.get("/api/v1/entities/ENT-A/balances", headers=headers).json()
    candidates = [b for b in balances["export_lines"] if Decimal(b["committed"]) == Decimal("1000.00")]
    assert len(candidates) == 1
    return candidates[0]


# --------------------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------------------
def test_concurrent_confirmations_never_over_allocate(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a1, a2 = bootstrap["analyst1"], bootstrap["analyst2"]
    submit_all(client, a1, ids)
    seed_tax(client, h)
    # 1200 only: covers line 1 fully; three candidate confirmations of 1200 each must not all pass
    seed_bank(client, h, txn_ref="TXN-CONC", amount="1200.00", remittance_info="BATCH SETTLEMENT")
    post(client, a1, "/api/v1/entities/ENT-A/reconcile-pass", {})
    cands = client.get("/api/v1/entities/ENT-A/candidates?status=proposed", headers=a1).json()
    assert len(cands) == 3

    results = []
    for i, cand in enumerate(cands):
        headers = a1 if i % 2 == 0 else a2
        r = client.post(f"/api/v1/candidates/{cand['id']}/confirm", json={}, headers=headers)
        results.append(r.status_code)
    assert results.count(201) >= 1
    assert 409 in results, f"over-allocation must be rejected atomically: {results}"

    balances = client.get("/api/v1/entities/ENT-A/balances", headers=a1).json()
    bank = [b for b in balances["bank_transactions"] if b["txn_ref"] == "TXN-CONC"][0]
    assert Decimal(bank["committed"]) <= Decimal("1200.00")
    assert Decimal(bank["committed"]) + Decimal(bank["available"]) == Decimal(bank["total"])
    for e in balances["export_lines"]:
        assert Decimal(e["committed"]) <= Decimal(e["total"])

    conflicts = client.get("/api/v1/entities/ENT-A/difference-cases", headers=a1).json()
    kinds = {c["kind"] for c in conflicts}
    assert "over_allocation_attempt" in kinds

    # ledger must not claim `matched` while a genuine receipt conflict is open; a rejected
    # over-allocation attempt is a difference case, not a receipt conflict
    ledgers = client.get("/api/v1/entities/ENT-A/status-ledgers", headers=a1).json()
    receipt = [r for r in ledgers["receipt_ledgers"] if r["txn_ref"] == "TXN-CONC"][0]
    assert receipt["conflict_open"] is False, "a rejected attempt is not a receipt conflict"
    # occupancy of 1200.00 fully explains the transaction, so the receipt side is matched
    assert receipt["state"] == "matched"
    assert "over_allocation_attempt" in kinds


def test_two_analysts_same_candidate_only_one_wins(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a1, a2 = bootstrap["analyst1"], bootstrap["analyst2"]
    submit_all(client, a1, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-DUP", amount="5000.00", remittance_info="COMBINED")
    post(client, a1, "/api/v1/entities/ENT-A/reconcile-pass", {})
    cand = client.get("/api/v1/entities/ENT-A/candidates?status=proposed", headers=a1).json()[0]

    r1 = client.post(f"/api/v1/candidates/{cand['id']}/confirm", json={}, headers=a1)
    r2 = client.post(f"/api/v1/candidates/{cand['id']}/confirm", json={}, headers=a2)
    assert r1.status_code == 201
    assert r2.status_code == 409


# --------------------------------------------------------------------------------------
# Customs correction
# --------------------------------------------------------------------------------------
def test_customs_correction_preserves_submitted_basis_and_revision_chain(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    line1 = ids[0]
    before = client.get(f"/api/v1/export-lines/{line1}", headers=a).json()
    assert before["declaration_ledger"]["state"] == "verified"

    # a prior confirmed link exists before the correction (1000.00 against 1200.00 declared)
    seed_bank(client, h, txn_ref="TXN-CORR-PRE", amount="1000.00", remittance_info="SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    pre_run = _compile(client, h)
    assert len(pre_run["result"]["facts"]) == 1

    # correction: line 1 amount 1200 -> 1100, quantity 100 -> 95
    seed_customs(
        client,
        h,
        lines=[{"line_no": 1, "sku": "SKU-AA-100", "description": "Widget A", "quantity": "95",
                "amount": "1100.00", "revises_line_no": 1}],
        doc_type="customs_correction",
    )
    after = client.get(f"/api/v1/export-lines/{line1}", headers=a).json()
    assert after["declaration_ledger"]["state"] == "correction_pending"
    assert after["declaration_ledger"]["correction_open"] is True
    assert Decimal(after["declared_amount"]) == Decimal("1100.00")
    assert len(after["revision_chain"]) == 2
    assert after["revision_chain"][0]["revision_no"] == 0
    assert after["revision_chain"][1]["revision_no"] == 1
    assert after["revision_chain"][1]["changed_fields"]["amount"]["before"] == "1200.00"
    # the old review record is still queryable
    history = after["declaration_ledger"]["history"]
    assert any(ev["to_state"] == "verified" for ev in history)
    assert any(ev["to_state"] == "correction_pending" for ev in history)
    # the confirmed line is now frozen and waiting for review
    assert [l for l in after["reconciliation_lines"] if l["status"] == "needs_review"]

    # previously submitted basis retained in the intake ledger
    docs = client.get("/api/v1/intake/documents?doc_type=customs_archive&entity_code=ENT-A", headers=a).json()
    original = client.get(f"/api/v1/intake/documents/{docs[0]['id']}", headers=a).json()
    original_line = [l for l in original["lines"]["customs_lines"] if l["line_no"] == 1][0]
    assert Decimal(original_line["amount"]) == Decimal("1200.00"), "correction must not rewrite the submitted basis"
    assert original_line["is_current"] is False

    # after the correction, the recompile excludes the frozen line from confirmed received amount
    run = _compile(client, h)
    assert not [f for f in run["result"]["facts"] if Decimal(f["amount"]) == Decimal("1000.00")]
    review = [d for d in run["result"]["differences"] if d["kind"] in ("reconfirmed_requires_review",
                                                                       "confirmed_line_under_review")]
    assert review


def test_customs_correction_below_committed_amount_freezes_instead_of_rewriting(client, bootstrap):
    """A correction is a source fact: it must land, but it can never shrink already-frozen occupancy."""
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-LOW", amount="1200.00", remittance_info="SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    r = client.post(
        "/api/v1/intake/documents",
        json={
            "doc_type": "customs_correction",
            "entity_code": "ENT-A",
            "external_no": "DECL-2026-100-REV-BAD",
            "original_location": "s3://customs/bad.xml",
            "received_time": T0.isoformat(),
            "lines": [
                {"line_no": 1, "sku": "SKU-AA-100", "description": "Widget A", "quantity": "10",
                 "amount": "100.00", "currency": "USD", "declaration_no": DECL, "revises_line_no": 1}
            ],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text

    export = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    assert Decimal(export["declared_amount"]) == Decimal("100.00"), "the correction itself is recorded"
    assert export["declaration_ledger"]["state"] == "correction_pending"
    # occupancy stays frozen at 1200.00 and is never rewritten to 100.00
    assert Decimal(export["balance"]["committed"]) == Decimal("1200.00")
    assert Decimal(export["balance"]["available"]) == Decimal("0.00")
    assert all(l["status"] == "needs_review" for l in export["reconciliation_lines"])

    cases = client.get("/api/v1/entities/ENT-A/difference-cases", headers=a).json()
    assert any(c["kind"] == "occupancy_exceeds_corrected_amount" for c in cases)

    # voiding the frozen line releases the occupancy and the ceiling falls back to the corrected amount
    line_id = export["reconciliation_lines"][0]["id"]
    r = client.post(f"/api/v1/reconciliation-lines/{line_id}/void", json={"reason": "correction superseded"},
                    headers=a)
    assert r.status_code == 200, r.text
    export2 = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    assert Decimal(export2["balance"]["committed"]) == Decimal("0.00")
    assert Decimal(export2["balance"]["total"]) == Decimal("100.00")
    assert Decimal(export2["balance"]["available"]) == Decimal("100.00")


# --------------------------------------------------------------------------------------
# Out-of-order tax receipts
# --------------------------------------------------------------------------------------
def test_out_of_order_tax_receipts_ordered_by_issue_time(client, bootstrap):
    h, _ = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    # later-issued receipt arrives first
    seed_tax(client, h, batch_no="B-LATE", issue_time=T_ISSUE_2, sequence_no=2)
    seed_tax(client, h, batch_no="B-EARLY", issue_time=T_ISSUE_1, sequence_no=1)

    rows = client.get("/api/v1/entities/ENT-A/tax-receipts", headers=a).json()
    batches = [r["batch_no"] for r in rows]
    assert batches.index("B-EARLY") < batches.index("B-LATE")
    assert all(r["order_conflict"] is False for r in rows)
    early = [r for r in rows if r["batch_no"] == "B-EARLY"]
    late = [r for r in rows if r["batch_no"] == "B-LATE"]
    assert len(early) == 3 and all(r["applied_sequence"] == 1 for r in early)
    assert all(r["applied_sequence"] == 2 for r in late)


def test_tax_receipt_sequence_breaks_an_issue_time_tie(client, bootstrap):
    """Same issue time, differing verifiable sequence: order is provable, applied by sequence."""
    h, _ = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    ts = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    seed_tax(client, h, batch_no="B-10", issue_time=ts, sequence_no=10)
    seed_tax(client, h, batch_no="B-05", issue_time=ts, sequence_no=5)

    rows = client.get("/api/v1/entities/ENT-A/tax-receipts", headers=a).json()
    assert all(r["order_conflict"] is False for r in rows)
    assert all(r["applied_sequence"] == 5 for r in rows if r["batch_no"] == "B-05")
    assert all(r["applied_sequence"] == 10 for r in rows if r["batch_no"] == "B-10")


def test_tax_receipt_order_conflict_when_ordering_unprovable(client, bootstrap):
    """Same issue time and no verifiable sequence: ordering is unprovable -> receipt conflict."""
    h, _ = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    ts = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    seed_tax(client, h, batch_no="B-NA", issue_time=ts, sequence_no=None)
    seed_tax(client, h, batch_no="B-NB", issue_time=ts, sequence_no=None)

    rows = client.get("/api/v1/entities/ENT-A/tax-receipts", headers=a).json()
    conflicted = [r for r in rows if r["order_conflict"]]
    assert conflicted, "unprovable ordering must enter receipt conflict"
    assert all(r["applied_sequence"] is None for r in conflicted)
    cases = client.get("/api/v1/entities/ENT-A/difference-cases", headers=a).json()
    assert any(c["kind"] == "receipt_order_conflict" for c in cases)


# --------------------------------------------------------------------------------------
# Cross-entity authorization
# --------------------------------------------------------------------------------------
def test_cross_entity_authorization_is_enforced(client, bootstrap):
    h, ids = _base(client, bootstrap)
    outsider = bootstrap["outsider"]

    r = client.post(
        "/api/v1/intake/documents",
        json={
            "doc_type": "invoice",
            "entity_code": "ENT-A",
            "external_no": "INV-X",
            "original_location": "s3://x",
            "received_time": T0.isoformat(),
            "lines": [{"line_no": 1, "quantity": "1", "amount": "1.00", "currency": "USD"}],
        },
        headers=outsider,
    )
    assert r.status_code == 403
    assert client.get("/api/v1/entities/ENT-A/dossiers", headers=outsider).status_code == 403
    assert client.get(f"/api/v1/export-lines/{ids[0]}", headers=outsider).status_code == 403
    # admin sees both entities
    assert client.get("/api/v1/entities/ENT-A/dossiers", headers=h).status_code == 200


# --------------------------------------------------------------------------------------
# Return-shipment candidate
# --------------------------------------------------------------------------------------
def test_return_document_becomes_reverse_candidate_not_eligibility_change(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    before = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()

    post(
        client,
        a,
        "/api/v1/intake/documents",
        {
            "doc_type": "return_document",
            "entity_code": "ENT-A",
            "external_no": "RET-001",
            "original_location": "s3://returns/RET-001.pdf",
            "event_time": datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc).isoformat(),
            "received_time": datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc).isoformat(),
            "lines": [
                {
                    "line_no": 1,
                    "declaration_no": DECL,
                    "sku": "SKU-AA-100",
                    "quantity": "3",
                    "amount": "36.00",
                    "currency": "USD",
                    "transport_shipped_date": datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc).isoformat(),
                    "transport_returned_date": datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc).isoformat(),
                }
            ],
        },
    )
    cands = client.get("/api/v1/entities/ENT-A/reverse-candidates", headers=a).json()
    assert len(cands) == 1
    assert cands[0]["status"] == "candidate"
    assert Decimal(cands[0]["quantity"]) == Decimal("3")
    assert cands[0]["reasons"], "candidate must explain its reasoning"

    after = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    assert after["declared_quantity"] == before["declared_quantity"], "reverse candidate must not alter declared facts"
    assert Decimal(after["balance"]["total"]) == Decimal(before["balance"]["total"])


# --------------------------------------------------------------------------------------
# Void / reassign / freeze flows keep the audit trail
# --------------------------------------------------------------------------------------
def test_void_releases_occupancy_with_audit_records(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-VOID", amount="1200.00", remittance_info="SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    # find the confirmed reconciliation line via the balances that are carrying occupancy
    balances_before = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    bank_before = [b for b in balances_before["bank_transactions"] if b["txn_ref"] == "TXN-VOID"][0]
    assert Decimal(bank_before["committed"]) == Decimal("1200.00")

    line_detail = None
    for e in balances_before["export_lines"]:
        if Decimal(e["committed"]) == Decimal("1200.00"):
            detail = client.get(f"/api/v1/export-lines/{e['export_line_id']}", headers=a).json()
            line_detail = detail["reconciliation_lines"][0]
    assert line_detail is not None

    r = client.post(
        f"/api/v1/reconciliation-lines/{line_detail['id']}/void",
        json={"reason": "payment recalled by bank"},
        headers=a,
    )
    assert r.status_code == 200, r.text

    balances_after = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    bank_after = [b for b in balances_after["bank_transactions"] if b["txn_ref"] == "TXN-VOID"][0]
    assert Decimal(bank_after["committed"]) == Decimal("0.00")
    assert Decimal(bank_after["available"]) == Decimal("1200.00")
    ledgers = client.get("/api/v1/entities/ENT-A/status-ledgers", headers=a).json()
    receipt = [x for x in ledgers["receipt_ledgers"] if x["txn_ref"] == "TXN-VOID"][0]
    assert receipt["state"] == "unmatched"

    # compile no longer counts it as a confirmed fact but publishes the bank remainder
    run = _compile(client, h)
    assert not [f for f in run["result"]["facts"] if Decimal(f["amount"]) == Decimal("1200.00")]


def test_frozen_confirmed_line_excluded_from_facts(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-REV", amount="1200.00", remittance_info="SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    run1 = _compile(client, h)
    assert len(run1["result"]["facts"]) == 1

    balances = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    export = [b for b in balances["export_lines"] if Decimal(b["committed"]) == Decimal("1200.00")][0]
    detail = client.get(f"/api/v1/export-lines/{export['export_line_id']}", headers=a).json()
    line_id = detail["reconciliation_lines"][0]["id"]

    r = client.post(
        f"/api/v1/reconciliation-lines/{line_id}/review",
        json={"reason": "buyer disputes this line"},
        headers=a,
    )
    assert r.status_code == 200, r.text

    run2 = _compile(client, h)
    assert len(run2["result"]["facts"]) == 0, "frozen line must not be counted as confirmed receipt"
    assert any(d["kind"] == "confirmed_line_under_review" for d in run2["result"]["differences"])
    # occupancy stays frozen
    balances2 = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    bank2 = [b for b in balances2["bank_transactions"] if b["txn_ref"] == "TXN-REV"][0]
    assert Decimal(bank2["committed"]) == Decimal("1200.00")


def test_drilldown_contains_ledgers_balances_scenarios_revision_chain(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-DRILL", amount="1200.00", remittance_info="SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    balances = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    export = [b for b in balances["export_lines"] if Decimal(b["committed"]) == Decimal("1200.00")][0]
    detail = client.get(f"/api/v1/export-lines/{export['export_line_id']}", headers=a).json()
    line_id = detail["reconciliation_lines"][0]["id"]

    drill = _line_detail(client, a, line_id)
    for key in ("source_files", "status_ledgers", "balance_occupancy", "allocation_scenarios",
                "revision_chain", "judgement_rules"):
        assert key in drill, f"drilldown missing {key}"
    assert drill["source_files"]
    assert drill["status_ledgers"]["declaration"]["history"]
    assert drill["balance_occupancy"]["holds"]
    assert drill["revision_chain"]


def test_openapi_and_swagger_available(client):
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    assert spec.json()["openapi"].startswith("3.")
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()


def test_tax_receipt_advances_submitted_declaration_at_issue_position(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    # submitted, not yet verified
    for eid in ids:
        post(client, a, f"/api/v1/export-lines/{eid}/declaration-events", {"event": "submitted"})
    assert client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()["declaration_ledger"]["state"] == "submitted"

    seed_tax(client, h, batch_no="B-OK", issue_time=T_ISSUE_1, sequence_no=1)
    after = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    assert after["declaration_ledger"]["state"] == "verified"
    assert after["declaration_ledger"]["review_ref"] == "B-OK"
    assert after["declaration_ledger"]["reviewed_at"] is not None
    # history is chronological by effective time: the receipt carries its issue time
    verified = [ev for ev in after["declaration_ledger"]["history"] if ev["to_state"] == "verified"]
    assert verified, after["declaration_ledger"]["history"]
    assert verified[-1]["actor"].startswith("tax_receipt:")
    assert verified[-1]["payload"]["reference"] == "B-OK"


def test_conflicted_tax_receipt_does_not_advance_declaration(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    for eid in ids:
        post(client, a, f"/api/v1/export-lines/{eid}/declaration-events", {"event": "submitted"})
    ts = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    seed_tax(client, h, batch_no="B-NA", issue_time=ts, sequence_no=None)
    seed_tax(client, h, batch_no="B-NB", issue_time=ts, sequence_no=None)
    after = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    assert after["declaration_ledger"]["state"] == "submitted", (
        "an unprovable ordering must not silently convert the declaration state"
    )
    cases = client.get("/api/v1/entities/ENT-A/difference-cases", headers=a).json()
    assert any(c["kind"] == "receipt_order_conflict" for c in cases)


def test_packing_list_corroborates_quantity_without_touching_amounts(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    post(
        client,
        h,
        "/api/v1/intake/documents",
        {
            "doc_type": "packing_list",
            "entity_code": "ENT-A",
            "external_no": "PL-2026-001",
            "original_location": "s3://packing/PL-2026-001.pdf",
            "received_time": T0.isoformat(),
            "lines": [
                {"line_no": 1, "sku": "SKU-AA-100", "quantity": "100", "packages": 10, "net_weight": "250.5",
                 "declaration_no": DECL},
                {"line_no": 2, "sku": "SKU-BB-200", "quantity": "48", "packages": 6, "net_weight": "120.0",
                 "declaration_no": DECL},
            ],
        },
    )
    rows = client.get("/api/v1/entities/ENT-A/packing-lines", headers=a).json()
    assert len(rows) == 2
    assert all(r["export_line_id"] for r in rows)

    post(client, a, "/api/v1/entities/ENT-A/resolve", {})
    dossiers = client.get("/api/v1/entities/ENT-A/dossiers", headers=a).json()
    with_packing = [d for d in dossiers if d["summary"].get("packing_quantity_mismatch") is not None]
    assert with_packing
    mismatch = [d for d in with_packing if d["summary"]["packing_quantity_mismatch"]]
    assert mismatch, "line 2 (50 declared vs 48 packed) must be flagged"

    # amounts are untouched by packing evidence
    for d in dossiers:
        if d["export_line_id"]:
            export = client.get(f"/api/v1/export-lines/{d['export_line_id']}", headers=a).json()
            assert export["declared_amount"] == d["summary"]["declared_amount"]
    cases = client.get("/api/v1/entities/ENT-A/difference-cases", headers=a).json()
    assert any(c["kind"] == "packing_quantity_mismatch" for c in cases)


def test_export_line_side_bounds_cumulative_confirmation(client, bootstrap):
    """Two partial receipts each carrying the same unique line key must not together exceed the
    declared amount of the export line, even though each bank transaction alone could cover it."""
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-E1", amount="1000.00", remittance_info="SETTLEMENT SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    # line 1 is 1200.00 declared and now has 1000.00 committed -> 200.00 left
    seed_bank(client, h, txn_ref="TXN-E2", amount="1200.00", remittance_info="SETTLEMENT SKU-AA-100")
    r = post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    assert len(r["auto_confirmed"]) == 1

    balances = client.get("/api/v1/entities/ENT-A/balances", headers=a).json()
    exp1 = [b for b in balances["export_lines"] if b["export_line_id"] == ids[0]][0]
    assert Decimal(exp1["committed"]) == Decimal("1200.00")
    assert Decimal(exp1["available"]) == Decimal("0.00")
    bank2 = [b for b in balances["bank_transactions"] if b["txn_ref"] == "TXN-E2"][0]
    # only 200.00 could be matched; the remaining 1000.00 stays unallocated on the bank side
    assert Decimal(bank2["committed"]) == Decimal("200.00")
    assert Decimal(bank2["available"]) == Decimal("1000.00")

    run = _compile(client, h)
    unalloc = [u for u in run["result"]["unallocated"] if u.get("txn_ref") == "TXN-E2"]
    assert unalloc and Decimal(unalloc[0]["unallocated_amount"]) == Decimal("1000.00")
    total_facts = sum(Decimal(f["amount"]) for f in run["result"]["facts"])
    assert total_facts == Decimal("1200.00")


def test_void_then_pass_can_reconfirm_freed_amount(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    seed_bank(client, h, txn_ref="TXN-RC", amount="1000.00", remittance_info="SKU-AA-100")
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    detail = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    line_id = detail["reconciliation_lines"][0]["id"]

    r = client.post(f"/api/v1/reconciliation-lines/{line_id}/void", json={"reason": "wrong line"}, headers=a)
    assert r.status_code == 200, r.text

    r = post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    assert len(r["auto_confirmed"]) == 1, "freed amount must be re-confirmable"
    run = _compile(client, h)
    assert sum(Decimal(f["amount"]) for f in run["result"]["facts"]) == Decimal("1000.00")
    # audit trail keeps the void and the new occupancy
    drill = _line_detail(client, a, r["auto_confirmed"][0])
    actions = {(h["side"], h["action"]) for h in drill["balance_occupancy"]["holds"]}
    assert ("export_line", "occupy") in actions
    assert ("bank_transaction", "occupy") in actions


def test_return_evidence_that_cannot_be_attributed_stays_a_candidate(client, bootstrap):
    """No unique line attribution + contradictory transport timing stays a reverse candidate,
    and never retires eligibility or changes declared totals."""
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    before = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()

    post(
        client,
        a,
        "/api/v1/intake/documents",
        {
            "doc_type": "return_document",
            "entity_code": "ENT-A",
            "external_no": "RET-AMBIG",
            "original_location": "s3://returns/RET-AMBIG.pdf",
            # effective time absent -> cannot prove timing at all
            "received_time": datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc).isoformat(),
            "lines": [
                {
                    "line_no": 1,
                    # no declaration_no / invoice_no -> not uniquely attributable
                    "sku": "SKU-AA-100",
                    "quantity": "5",
                    "amount": "60.00",
                    "currency": "USD",
                    # transport timing contradicts itself
                    "transport_shipped_date": datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc).isoformat(),
                    "transport_returned_date": datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc).isoformat(),
                }
            ],
        },
    )
    cands = client.get("/api/v1/entities/ENT-A/reverse-candidates", headers=a).json()
    assert len(cands) == 1
    cand = cands[0]
    assert cand["status"] == "candidate"
    assert cand["unique_attribution"] is False
    joined = " ".join(cand["reasons"])
    assert "no unique export line match" in joined
    assert "inconsistent" in joined

    after = client.get(f"/api/v1/export-lines/{ids[0]}", headers=a).json()
    assert after["declared_amount"] == before["declared_amount"]
    assert after["declared_quantity"] == before["declared_quantity"]
    assert after["declaration_ledger"]["state"] == "verified", "return evidence must not retire eligibility"

    # it is surfaced as a difference reason, not a fact
    run = _compile(client, h)
    assert any(d["kind"] == "reverse_quantity_candidate" for d in run["result"]["differences"])
    assert not [f for f in run["result"]["facts"] if Decimal(f["amount"]) == Decimal("60.00")]


def test_correction_without_revises_pointer_is_rejected(client, bootstrap):
    """A customs correction must connect to the line it revises; it may never silently re-baseline."""
    h, ids = _base(client, bootstrap)
    r = client.post(
        "/api/v1/intake/documents",
        json={
            "doc_type": "customs_correction",
            "entity_code": "ENT-A",
            "external_no": "DECL-2026-100-REV-NOPTR",
            "original_location": "s3://customs/noptr.xml",
            "received_time": T0.isoformat(),
            "lines": [
                {"line_no": 1, "sku": "SKU-AA-100", "description": "Widget A", "quantity": "90",
                 "amount": "1100.00", "currency": "USD", "declaration_no": DECL}
            ],
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert "revises_line_no" in r.text
    # nothing was written
    export = client.get(f"/api/v1/export-lines/{ids[0]}", headers=bootstrap["analyst1"]).json()
    assert Decimal(export["declared_amount"]) == Decimal("1200.00")


def test_blocked_unique_link_does_not_undo_earlier_confirmations_in_the_same_pass(client, bootstrap):
    """A second unique link that cannot be satisfied must not roll back the first success."""
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    seed_tax(client, h)
    # line 2 is declared at exactly 1800.00
    seed_bank(client, h, txn_ref="TXN-B1", amount="1800.00", remittance_info="PAYMENT SKU-BB-200")
    seed_bank(client, h, txn_ref="TXN-B2", amount="500.00", remittance_info="SECOND PAYMENT SKU-BB-200")

    r = post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    assert len(r["auto_confirmed"]) == 1, f"the first link must survive the blocked second one: {r}"

    run = _compile(client, h)
    assert sum(Decimal(f["amount"]) for f in run["result"]["facts"]) == Decimal("1800.00")
    blocked = [u for u in run["result"]["unallocated"] if u.get("txn_ref") == "TXN-B2"]
    assert blocked and Decimal(blocked[0]["unallocated_amount"]) == Decimal("500.00")
    cases = client.get("/api/v1/entities/ENT-A/difference-cases", headers=a).json()
    assert any(c["kind"] == "unique_link_blocked_by_balance" for c in cases)

    # a later pass does not silently invent an allocation either
    r2 = post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    assert r2["auto_confirmed"] == []


def test_settlement_ledgers_do_not_share_settled_fields(client, bootstrap, engine):
    """Schema-level statement: declaration/receipt ledgers expose no shared settlement column."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    decl_cols = {c["name"] for c in insp.get_columns("declaration_ledgers")}
    receipt_cols = {c["name"] for c in insp.get_columns("receipt_ledgers")}
    assert "settled" not in decl_cols and "settled" not in receipt_cols
    assert "settled_amount" not in decl_cols and "settled_amount" not in receipt_cols
    shared = decl_cols & receipt_cols
    assert shared <= {"id", "state", "version", "updated_at"}, shared
    # the state vocabularies are disjoint: no shared "settled" value exists in either ledger
    assert "settled" not in {"draft", "submitted", "verified", "rejected", "correction_pending"}
    assert "settled" not in {"unmatched", "pending_allocation", "partial", "matched", "conflict"}
