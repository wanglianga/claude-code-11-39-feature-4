"""End-to-end tests for the tax-review → receipt timeliness & status-credibility service.

Covers the spec branches:
  * three-sample split (first receipt / full receipt / unfinished observation, never mixed)
  * pending-allocation flows need an auditable line-level basis; same account/currency is not proof
  * bank coverage horizon drives the observation boundary; uncovered post-approval lines are not waits
  * late receipts open a new run and never rewrite historical runs
  * revocation three branches (post-cutoff kept / retroactive removed / ambiguous conflict)
  * equal-issue-time sequence collisions freeze the line (no duration generated)
  * coverage corrections reduce scope for new runs while historical runs stay versioned
  * Kaplan–Meier risk sets derived from the actual review/partial/full/coverage events
  * median suppression when too few events complete; zero-day results are never fabricated
  * same-day event/cutoff ambiguity is reported with both boundary outcomes
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app import models as m
from app.services import ledgers
from helpers import (
    DECL,
    export_line_ids,
    post,
    seed_bank,
    seed_contract,
    seed_customs,
    seed_invoice,
    submit_all,
)

ACCOUNT = "ACCT-MAIN-USD"
CURRENCY = "USD"


# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------

def _dt(y, mo, d, h=8, minute=0):
    return datetime(y, mo, d, h, minute, tzinfo=timezone.utc)


def _parse(value):
    return datetime.fromisoformat(value)


def _base(client, boot):
    h = boot["admin"]
    seed_contract(client, h)
    seed_invoice(client, h)
    seed_customs(client, h)
    ids = export_line_ids(client, boot["analyst1"])
    assert len(ids) == 3
    return h, ids


def add_coverage(client, h, coverage_date, status="covered", batch="EX-2026-09",
                 completion="1.0000", entity="ENT-A"):
    r = client.post(
        "/api/v1/timeliness/coverage",
        json={
            "entity_code": entity,
            "bank_account": ACCOUNT,
            "currency": CURRENCY,
            "coverage_date": coverage_date.isoformat(),
            "status": status,
            "completion": completion,
            "extraction_batch_no": batch,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def add_coverage_span(client, h, dates, status="covered", batch="EX-2026-09"):
    return [add_coverage(client, h, d, status=status, batch=batch) for d in dates]


def tax_receipt(client, h, batch_no, issue_time, received_time=None, sequence_no=None,
                review_status="approved", declaration_no=DECL):
    body = {
        "doc_type": "tax_receipt",
        "entity_code": "ENT-A",
        "external_no": batch_no,
        "original_location": f"s3://tax/{batch_no}.json",
        "event_time": issue_time.isoformat(),
        "received_time": (received_time or issue_time).isoformat(),
        "lines": [
            {
                "line_no": i + 1,
                "batch_no": batch_no,
                "declaration_no": declaration_no,
                "review_status": review_status,
                "quantity": qty,
                "amount": amount,
                "currency": "USD",
                "issue_time": issue_time.isoformat(),
                "sequence_no": sequence_no,
            }
            for i, (qty, amount) in enumerate((("100", "1200.00"), ("50", "1800.00"), ("20", "2000.00")))
        ],
    }
    return post(client, h, "/api/v1/intake/documents", body)


def run_analysis(client, h, cutoff, **extra):
    body = {"entity_code": "ENT-A", "observation_cutoff": cutoff.isoformat()}
    body.update(extra)
    r = client.post("/api/v1/timeliness/runs", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def finding(run, export_line_id):
    rows = [f for f in run["result"]["line_findings"] if f["export_line_id"] == export_line_id]
    assert rows, f"export line {export_line_id} missing from run"
    return rows[0]


def set_confirmed_at(db, bank_txn_ref, when):
    txn = db.scalar(select(m.BankTransaction).where(m.BankTransaction.txn_ref == bank_txn_ref))
    lines = db.scalars(
        select(m.ReconciliationLine).where(m.ReconciliationLine.bank_transaction_id == txn.id)
    ).all()
    for l in lines:
        l.confirmed_at = when
    db.commit()
    return [l.id for l in lines]


def stratum_for(run, batch_no):
    rows = [s for s in run["result"]["strata"] if s["review_batch_no"] == batch_no]
    assert rows
    return rows[0]


def _revocation(client, h, *, issued, received=None, effective=None, retroactive=False,
                target_batch="B-2026-09-10", target_export_line_id=None, seq=9):
    body = {
        "entity_code": "ENT-A",
        "external_no": f"REV-{target_batch}-{seq}",
        "original_location": "s3://tax/revocation.pdf",
        "target_batch_no": target_batch,
        "issued_at": issued.isoformat(),
        "received_at": (received or issued).isoformat(),
        "effective_at": (effective or received or issued).isoformat(),
        "retroactive": retroactive,
        "sequence_no": seq,
    }
    if target_export_line_id is not None:
        body["target_export_line_id"] = target_export_line_id
    r = client.post("/api/v1/timeliness/revocations", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------------------
# 1. Three-sample split: full receipt / partial (first only) / unfinished observation
# --------------------------------------------------------------------------------------

def test_three_way_sample_split_and_line_drilldown(client, bootstrap, db):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-09-10", approval, sequence_no=1)

    # full receipt for line 1 (unique SKU reference, 1200 == declared 1200)
    seed_bank(client, h, txn_ref="TXN-FULL", amount="1200.00",
              remittance_info="SETTLE SKU-AA-100", value_date=_dt(2026, 9, 18))
    # combined remittance: partial confirmation for line 2, proposed candidate stays for line 3
    seed_bank(client, h, txn_ref="TXN-COMB", amount="3000.00",
              remittance_info="CONSOLIDATED PAYMENT", value_date=_dt(2026, 9, 19))
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    cands = client.get("/api/v1/entities/ENT-A/candidates?status=proposed", headers=a).json()
    line2_cand = next(c for c in cands if c["export_line_id"] == ids[1])
    r = client.post(
        f"/api/v1/candidates/{line2_cand['id']}/confirm",
        json={"amount": "1000.00", "note": "partial receipt for line 2"},
        headers=a,
    )
    assert r.status_code == 201, r.text

    set_confirmed_at(db, "TXN-FULL", _dt(2026, 9, 20))
    set_confirmed_at(db, "TXN-COMB", _dt(2026, 9, 25))

    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 9, 30), _dt(2026, 10, 31),
                                  _dt(2026, 11, 30), _dt(2026, 12, 31)])
    run = run_analysis(client, h, _dt(2026, 12, 15))

    counts = run["result"]["sample_counts"]
    assert counts["first_receipt_completed"] == 2
    assert counts["full_receipt_completed"] == 1
    assert counts["unfinished_observation"] == 1
    assert counts["excluded_not_sampled"] == 0

    f1 = finding(run, ids[0])
    assert f1["sample_classification"] == "full_complete"
    assert f1["in_first_receipt_sample"] is True
    assert f1["in_full_receipt_sample"] is True
    assert Decimal(f1["first_receipt_days"]) == Decimal("10.0000")
    assert Decimal(f1["full_receipt_days"]) == Decimal("10.0000")
    assert f1["credibility_tier"] == "analyzable"

    f2 = finding(run, ids[1])
    assert f2["sample_classification"] == "first_complete"
    assert f2["in_first_receipt_sample"] is True
    assert f2["in_full_receipt_sample"] is False, "partial receipt must not enter the full-receipt sample"
    assert Decimal(f2["first_receipt_days"]) == Decimal("15.0000")
    assert f2["full_receipt_days"] is None

    f3 = finding(run, ids[2])
    assert f3["sample_classification"] == "unfinished_censored"
    assert f3["in_first_receipt_sample"] is False
    assert f3["in_full_receipt_sample"] is False
    assert f3["pending_evidence_exists"] is True, "proposed candidate is an auditable line-level basis"
    assert Decimal(f3["waiting_days"]) > 0
    # unfinished observations live in their own distribution, never in completed ones
    strata = stratum_for(run, "B-2026-09-10")
    assert strata["first_receipt_distribution"]["count"] == 2
    assert strata["full_receipt_distribution"]["count"] == 1
    assert strata["waiting_time_distribution"]["count"] == 1
    assert strata["unfinished_observation_count"] == 1

    # drilldown: receipt sequence, coverage boundary, attribution, candidate basis, qualification
    d = client.get(f"/api/v1/timeliness/runs/{run['run_id']}/lines/{ids[2]}", headers=h).json()
    assert d["sample_qualification"]["sample_classification"] == "unfinished_censored"
    assert d["bank_coverage"]["bank_uncovered"] is False
    assert d["bank_coverage"]["horizon"] is not None
    pending = d["receipt_attribution"]["pending_allocation_flows"]
    with_basis = [p for p in pending if p.get("evidence_exists")]
    assert with_basis and with_basis[0]["basis"] == "reconciliation_candidate"
    assert any(ev["review_status"] == "approved" for ev in d["review_event_sequence"])

    # main task's versioned declaration status is part of the drilldown
    assert d["declaration_status"]["state"] == "verified"
    states = [ev["to_state"] for ev in d["declaration_status"]["history"]]
    assert "submitted" in states and "verified" in states

    # the eight spec cases are explicitly distinguishable
    assert d["case_flags"]["unfinished_waiting"] is True
    assert d["case_flags"]["pending_allocation_receipt"] is True
    assert d["case_flags"]["first_receipt"] is False
    assert d["case_flags"]["full_receipt"] is False
    assert d["case_flags"]["bank_uncovered"] is False
    assert d["case_flags"]["event_conflict"] is False
    assert d["case_flags"]["late_event"] is False
    assert d["case_flags"]["receipt_revocation"] is False
    # a fully received line carries the receipt flags instead
    d1 = client.get(f"/api/v1/timeliness/runs/{run['run_id']}/lines/{ids[0]}", headers=h).json()
    assert d1["case_flags"]["full_receipt"] is True and d1["case_flags"]["first_receipt"] is True
    assert d1["case_flags"]["unfinished_waiting"] is False


# --------------------------------------------------------------------------------------
# 2. Same account/currency unallocated flow without a line-level basis is NOT evidence
# --------------------------------------------------------------------------------------

def test_pending_flow_without_line_basis_is_not_evidence(client, bootstrap, db):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-09-10", approval, sequence_no=1)

    seed_bank(client, h, txn_ref="TXN-PEND", amount="2000.00",
              remittance_info="CONSOLIDATED PAYMENT", value_date=_dt(2026, 9, 19))
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    cands = client.get("/api/v1/entities/ENT-A/candidates?status=proposed", headers=a).json()

    # partially confirm line 2, then decline every remaining proposed candidate so the
    # transaction still carries unallocated funds but no line-level basis points at line 3
    line2 = next(c for c in cands if c["export_line_id"] == ids[1])
    r = client.post(f"/api/v1/candidates/{line2['id']}/confirm",
                    json={"amount": "500.00"}, headers=a)
    assert r.status_code == 201, r.text
    remaining = db.scalars(
        select(m.ReconciliationCandidate).where(
            m.ReconciliationCandidate.bank_transaction_id ==
            db.scalar(select(m.BankTransaction.id).where(m.BankTransaction.txn_ref == "TXN-PEND")),
            m.ReconciliationCandidate.status == "proposed",
        )
    ).all()
    for c in remaining:
        c.status = "declined"
    txn = db.scalar(select(m.BankTransaction).where(m.BankTransaction.txn_ref == "TXN-PEND"))
    ledgers.refresh_receipt_state(db, txn)
    db.commit()

    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 12, 31)])
    run = run_analysis(client, h, _dt(2026, 12, 15))
    f3 = finding(run, ids[2])
    assert f3["pending_evidence_exists"] is False
    assert f3["sample_classification"] == "unfinished_censored"
    assert f3["in_first_receipt_sample"] is False
    assert f3["in_full_receipt_sample"] is False

    d = client.get(f"/api/v1/timeliness/runs/{run['run_id']}/lines/{ids[2]}", headers=h).json()
    no_basis = [p for p in d["receipt_attribution"]["pending_allocation_flows"]
                if p["evidence_exists"] is False]
    assert no_basis and "without line-level" in no_basis[0]["reason"]


# --------------------------------------------------------------------------------------
# 3. Bank data stops before the approval date: uncovered, not waiting, not delay
# --------------------------------------------------------------------------------------

def test_bank_uncovered_after_approval_is_data_pending(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 10, 20)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-10-20", approval, sequence_no=1)
    # coverage ends 2026-10-15, before the approval
    add_coverage_span(client, h, [_dt(2026, 9, 1), _dt(2026, 10, 15)], batch="EX-2026-10")
    run = run_analysis(client, h, _dt(2026, 12, 15))
    for f in run["result"]["line_findings"]:
        assert f["bank_uncovered"] is True
        assert f["credibility_tier"] == "data_pending"
        assert f["sample_classification"] == "excluded_not_sampled"
        assert f["waiting_days"] is None, "uncovered lines are never counted as waiting time"
    assert run["result"]["sample_counts"]["unfinished_observation"] == 0
    # no stratum/KM risk set may include uncovered lines
    assert run["result"]["strata"] == []


# --------------------------------------------------------------------------------------
# 4. Late receipt: new run incorporates and flags it; historical run stays untouched
# --------------------------------------------------------------------------------------

def test_late_receipt_creates_new_run_without_rewriting_history(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    add_coverage_span(client, h, [_dt(2026, 9, 1), _dt(2026, 12, 31)])

    # run 1 at 2026-09-30: approval evidence does not exist yet
    run1 = run_analysis(client, h, _dt(2026, 9, 30))
    f_before = finding(run1, ids[0])
    assert f_before["credibility_tier"] == "data_pending"
    assert f_before["approved_at"] is None

    # receipt issued 2026-09-10 (before the run-1 cutoff) but only received 2026-10-15 (after it)
    tax_receipt(client, h, "B-LATE", _dt(2026, 9, 10),
                received_time=_dt(2026, 10, 15), sequence_no=1)

    run2 = run_analysis(client, h, _dt(2026, 12, 15))
    f_after = finding(run2, ids[0])
    assert f_after["approved_at"] is not None
    assert f_after["detail"]["late_events"], "late receipt must be explicitly flagged"
    assert f_after["credibility_tier"] == "restricted"

    # historical run is the same immutable snapshot
    r1_again = client.get(f"/api/v1/timeliness/runs/{run1['run_id']}", headers=h).json()
    assert r1_again["input_fingerprint"] == run1["input_fingerprint"]
    f_hist = finding(r1_again, ids[0])
    assert f_hist["approved_at"] is None
    assert run1["input_fingerprint"] != run2["input_fingerprint"]

    records = client.get(
        "/api/v1/timeliness/event-records?entity_code=ENT-A&record_kind=tax_receipt", headers=h
    ).json()
    assert any(r["late_flag"] is True for r in records)


# --------------------------------------------------------------------------------------
# 5. Revocation notice branches
# --------------------------------------------------------------------------------------

def test_revocation_effective_after_cutoff_keeps_historical_sample(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-09-10", approval, sequence_no=1)
    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 12, 31)])
    cutoff = _dt(2026, 10, 31)
    # revocation issued and effective after the cutoff, non-retroactive
    _revocation(client, h, issued=_dt(2026, 11, 5), effective=_dt(2026, 11, 5), seq=2)
    run = run_analysis(client, h, cutoff)
    f = finding(run, ids[0])
    assert f["revocation_status"] == "effective_after_cutoff_kept"
    assert f["approved_at"] is not None
    assert f["sample_classification"] == "unfinished_censored"


def test_retroactive_revocation_removes_approval_from_new_run(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-09-10", approval, sequence_no=1)
    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 12, 31)])
    run_before = run_analysis(client, h, _dt(2026, 10, 31))
    assert finding(run_before, ids[0])["approved_at"] is not None

    _revocation(client, h, issued=_dt(2026, 11, 2), effective=_dt(2026, 9, 10),
                retroactive=True, seq=3)
    run_after = run_analysis(client, h, _dt(2026, 12, 15))
    f = finding(run_after, ids[0])
    assert f["revocation_status"] == "retroactive_removed"
    assert f["approved_at"] is None
    assert f["sample_classification"] == "excluded_not_sampled"
    # the batch-targeted revocation fans out to every line of the batch and is visible
    # in the drilldown sequence, even though it was bound to a single line on registration
    d3 = client.get(f"/api/v1/timeliness/runs/{run_after['run_id']}/lines/{ids[2]}", headers=h).json()
    assert any(ev["kind"] == "revocation_notice" for ev in d3["review_event_sequence"])
    assert d3["sample_qualification"]["sample_classification"] == "excluded_not_sampled"
    # prior version remains intact
    kept = client.get(f"/api/v1/timeliness/runs/{run_before['run_id']}", headers=h).json()
    assert finding(kept, ids[0])["approved_at"] is not None


def test_ambiguous_revocation_freezes_line(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-09-10", approval, sequence_no=1)
    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 12, 31)])
    # the notice points at an unknown batch but is explicitly bound to line 1; the registry
    # cannot resolve the target event, so the line is frozen rather than silently de-approving
    out = _revocation(client, h, issued=_dt(2026, 10, 1), effective=_dt(2026, 10, 2),
                      target_batch="B-DOES-NOT-EXIST", target_export_line_id=ids[0], seq=5)
    assert out["target_ambiguous"] is True

    run = run_analysis(client, h, _dt(2026, 12, 15))
    f = finding(run, ids[0])
    assert f["credibility_tier"] == "event_conflict"
    assert f["sample_classification"] == "excluded_not_sampled"
    assert f["first_receipt_days"] is None and f["full_receipt_days"] is None


# --------------------------------------------------------------------------------------
# 6. Equal issue time with unresolvable sequence freezes the review conclusion
# --------------------------------------------------------------------------------------

def test_sequence_collision_freezes_conclusion(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    tie = _dt(2026, 9, 12, 9, 0)
    # two batches, same issue time, no verifiable sequence
    tax_receipt(client, h, "B-TIE-1", tie, sequence_no=None)
    tax_receipt(client, h, "B-TIE-2", tie, sequence_no=None)
    add_coverage_span(client, h, [_dt(2026, 9, 1), _dt(2026, 12, 31)])
    run = run_analysis(client, h, _dt(2026, 12, 15))
    for f in run["result"]["line_findings"]:
        assert f["credibility_tier"] == "event_conflict", f["export_line_id"]
        assert f["sample_classification"] == "excluded_not_sampled"
        assert f["first_receipt_days"] is None
    assert run["result"]["tier_counts"]["event_conflict"] == 3


# --------------------------------------------------------------------------------------
# 7. Coverage correction reduces scope for new runs; historical run versioned
# --------------------------------------------------------------------------------------

def test_coverage_correction_reduces_scope_but_keeps_history(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-2026-09-10", approval, sequence_no=1)
    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 10, 31), _dt(2026, 11, 30)],
                      batch="EX-ORIG")
    run1 = run_analysis(client, h, _dt(2026, 11, 30))
    assert finding(run1, ids[0])["bank_uncovered"] is False

    corr = client.post(
        "/api/v1/timeliness/coverage/corrections",
        json={
            "entity_code": "ENT-A",
            "bank_account": ACCOUNT,
            "currency": CURRENCY,
            "date_from": _dt(2026, 10, 1).isoformat(),
            "date_to": _dt(2026, 11, 30).isoformat(),
            "new_status": "uncovered",
            "new_completion": "0.0000",
            "extraction_batch_no": "EX-CORR-1",
            "note": "extraction batch re-issued with reduced coverage",
        },
        headers=h,
    )
    assert corr.status_code == 201, corr.text
    assert corr.json()["reduced_coverage_dates"], "covered -> uncovered must be reported"

    run2 = run_analysis(client, h, _dt(2026, 12, 15))
    f2 = finding(run2, ids[0])
    assert f2["bank_uncovered"] is True
    assert f2["credibility_tier"] == "data_pending"

    kept = client.get(f"/api/v1/timeliness/runs/{run1['run_id']}", headers=h).json()
    assert finding(kept, ids[0])["bank_uncovered"] is False

    current = client.get("/api/v1/timeliness/coverage?entity_code=ENT-A", headers=h).json()
    by_date = {_parse(row["coverage_date"]): row["status"] for row in current}
    assert by_date[_dt(2026, 9, 10)] == "covered"
    assert by_date[_dt(2026, 10, 31)] == "uncovered"
    assert by_date[_dt(2026, 11, 30)] == "uncovered"


# --------------------------------------------------------------------------------------
# 7b. Partial coverage: wait window ends at the last fully-covered horizon, not the partial day
# --------------------------------------------------------------------------------------

def test_partial_coverage_does_not_extend_wait_window(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-PART", approval, sequence_no=1)
    # fully covered through 2026-09-30, then only partial extraction on 2026-10-31
    add_coverage(client, h, _dt(2026, 9, 10), batch="EX-P1")
    add_coverage(client, h, _dt(2026, 9, 30), batch="EX-P1")
    add_coverage(client, h, _dt(2026, 10, 31), status="partial", completion="0.5000", batch="EX-P2")
    run = run_analysis(client, h, _dt(2026, 12, 15))
    f = finding(run, ids[0])
    assert f["bank_uncovered"] is False
    # the boundary is the last *fully covered* point (2026-09-30); partial extraction cannot extend it
    assert f["observation_boundary_at"] == _dt(2026, 9, 30).isoformat()
    assert f["boundary_basis"] == "bank_coverage_horizon"
    assert Decimal(f["waiting_days"]) == Decimal("20.0000")
    cal = client.get("/api/v1/timeliness/coverage?entity_code=ENT-A", headers=h).json()
    partial_rows = [r for r in cal if r["status"] == "partial"]
    assert partial_rows and Decimal(partial_rows[0]["completion"]) == Decimal("0.5000")


def test_event_registry_holds_all_four_evidence_families(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-EVID", _dt(2026, 9, 10), sequence_no=1)
    add_coverage(client, h, _dt(2026, 9, 30), batch="EX-EVID")
    _revocation(client, h, issued=_dt(2026, 10, 2), effective=_dt(2026, 10, 2),
                target_batch="B-EVID", seq=4)
    # tax receipts are projected from the main task into the registry when an analysis run reads them
    run_analysis(client, h, _dt(2026, 12, 15))
    kinds = {
        r["record_kind"]
        for r in client.get(
            "/api/v1/timeliness/event-records?entity_code=ENT-A", headers=h
        ).json()
    }
    assert {"tax_receipt", "revocation_notice", "bank_coverage_proof", "extraction_batch"} <= kinds


# --------------------------------------------------------------------------------------
# 7c. Correction pending: an open customs correction restricts the credibility tier
# --------------------------------------------------------------------------------------

def test_correction_pending_line_is_restricted(client, bootstrap):
    from helpers import seed_customs

    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 9, 10)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-CORR-PEND", approval, sequence_no=1)
    add_coverage_span(client, h, [_dt(2026, 9, 10), _dt(2026, 12, 31)], batch="EX-CP")
    before = run_analysis(client, h, _dt(2026, 12, 15))
    assert finding(before, ids[0])["credibility_tier"] == "analyzable"

    # a customs correction moves the declaration into correction_pending
    seed_customs(
        client,
        h,
        lines=[{
            "line_no": 1, "sku": "SKU-AA-100", "description": "Widget A", "quantity": "98",
            "amount": "1150.00", "revises_line_no": 1,
        }],
        doc_type="customs_correction",
    )
    after = run_analysis(client, h, _dt(2026, 12, 15))
    f = finding(after, ids[0])
    assert f["credibility_tier"] == "restricted"
    assert any("correction_pending" in r for r in f["limit_reasons"])


# --------------------------------------------------------------------------------------
# 8. Kaplan–Meier risk sets derived from actual events (first vs full, partial censoring)
# --------------------------------------------------------------------------------------

def test_km_risk_sets_derived_from_actual_events(client, bootstrap, db):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    t0 = _dt(2026, 11, 1)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-KM", t0, sequence_no=1)

    # line 1: full receipt at t+5 (unique SKU)
    seed_bank(client, h, txn_ref="TXN-KM-1", amount="1200.00",
              remittance_info="SKU-AA-100", value_date=t0 + timedelta(days=4))
    # combined txn: line 2 and line 3 each get a partial manual confirmation at t+7
    seed_bank(client, h, txn_ref="TXN-KM-2", amount="2800.00",
              remittance_info="CONSOLIDATED", value_date=t0 + timedelta(days=6))
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    cands = client.get("/api/v1/entities/ENT-A/candidates?status=proposed", headers=a).json()
    by_line = {c["export_line_id"]: c for c in cands}
    r2 = client.post(f"/api/v1/candidates/{by_line[ids[1]]['id']}/confirm",
                     json={"amount": "1000.00"}, headers=a)
    r3 = client.post(f"/api/v1/candidates/{by_line[ids[2]]['id']}/confirm",
                     json={"amount": "1000.00"}, headers=a)
    assert r2.status_code == 201 and r3.status_code == 201

    set_confirmed_at(db, "TXN-KM-1", t0 + timedelta(days=5))
    set_confirmed_at(db, "TXN-KM-2", t0 + timedelta(days=7))

    add_coverage_span(client, h, [t0, _dt(2026, 12, 31)], batch="EX-KM")
    cutoff = t0 + timedelta(days=20)
    run = run_analysis(client, h, cutoff, mature_horizon_days=15)
    strata = stratum_for(run, "B-KM")

    first = strata["kaplan_meier"]["first_receipt"]
    full = strata["kaplan_meier"]["full_receipt"]

    # first-receipt risk set: 3 lines, events at t=5 (L1), t=7 (L2, L3), no censoring
    assert first["risk_set_size"] == 3
    assert first["events"] == 3 and first["censored"] == 0
    pts = {Decimal(p["t_days"]): p for p in first["points"]}
    assert pts[Decimal("5.0000")]["n_risk"] == 3
    assert pts[Decimal("5.0000")]["d_events"] == 1
    assert Decimal(pts[Decimal("5.0000")]["survival"]) == Decimal("0.666667")
    assert pts[Decimal("7.0000")]["n_risk"] == 2
    assert pts[Decimal("7.0000")]["d_events"] == 2
    assert Decimal(pts[Decimal("7.0000")]["survival"]) == Decimal("0.000000")
    assert first["median_days"] == "7.0000"

    # full-receipt risk set: L1 event t=5; L2/L3 partial stay at risk and are censored t=20
    assert full["risk_set_size"] == 3
    assert full["events"] == 1 and full["censored"] == 2
    fpts = {Decimal(p["t_days"]): p for p in full["points"]}
    assert fpts[Decimal("5.0000")]["n_risk"] == 3
    assert Decimal(fpts[Decimal("5.0000")]["survival"]) == Decimal("0.666667")
    assert full["median_days"] is None, "survival stays above 0.5: median not yet reachable"
    assert "not yet reachable" in full["median_note"]

    # survival endpoint exposes the persisted points
    surv = client.get(f"/api/v1/timeliness/runs/{run['run_id']}/survival", headers=h).json()
    assert {p["metric"] for p in surv} == {"first_receipt", "full_receipt"}
    assert all(p["n_risk"] >= p["d_events"] for p in surv)

    # mature batch: 1 of 3 fully received, 2 of 3 still unfinished (partial counts as unfinished)
    assert Decimal(strata["mature_completed_proportion"]) == Decimal("0.3333")
    assert Decimal(strata["mature_unfinished_proportion"]) == Decimal("0.6667")


# --------------------------------------------------------------------------------------
# 9. No completion events: median suppressed, zero-day never fabricated
# --------------------------------------------------------------------------------------

def test_no_events_means_no_median_and_no_zero_day(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 11, 1)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-WAIT", approval, sequence_no=1)
    add_coverage_span(client, h, [approval, _dt(2026, 12, 31)], batch="EX-WAIT")
    run = run_analysis(client, h, approval + timedelta(days=20))
    strata = stratum_for(run, "B-WAIT")
    for metric in ("first_receipt", "full_receipt"):
        km = strata["kaplan_meier"][metric]
        assert km["events"] == 0
        assert km["median_days"] is None
        assert "zero-day" in km["median_note"]
        assert km["points"] == []
    waiting = strata["waiting_time_distribution"]
    assert waiting["count"] == 3
    assert all(Decimal(v) > 0 for v in waiting["values_days"]), \
        "waiting can never be zero-day by accident"


# --------------------------------------------------------------------------------------
# 10. Same-day event and cutoff with date-only precision: both boundaries reported
# --------------------------------------------------------------------------------------

def test_same_day_event_and_cutoff_emits_dual_boundary(client, bootstrap, db):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    approval = _dt(2026, 11, 1)
    submit_all(client, a, ids)
    tax_receipt(client, h, "B-SAME", approval, sequence_no=1)
    seed_bank(client, h, txn_ref="TXN-SAME", amount="1200.00",
              remittance_info="SKU-AA-100", value_date=_dt(2026, 11, 20))
    post(client, a, "/api/v1/entities/ENT-A/reconcile-pass", {})
    # date-only confirmation timestamp on the cutoff day
    set_confirmed_at(db, "TXN-SAME", datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc))
    add_coverage_span(client, h, [approval, _dt(2026, 12, 31)], batch="EX-SAME")
    cutoff = _dt(2026, 11, 21, 12, 0)
    run = run_analysis(client, h, cutoff)
    f = finding(run, ids[0])
    amb = f["same_day_ambiguity"]
    assert amb and amb["rule"] == "date-only event timestamp on the cutoff date"
    assert amb["as_recorded"]["receipt_counted"] is True
    assert "conservative_end_of_day" in amb


# --------------------------------------------------------------------------------------
# 11. Earlier rejection cannot override a later approval; later rejection withdraws it
# --------------------------------------------------------------------------------------

def test_replay_respects_issue_position_not_arrival(client, bootstrap):
    h, ids = _base(client, bootstrap)
    a = bootstrap["analyst1"]
    submit_all(client, a, ids)
    # rejection issued earlier, approval issued later
    tax_receipt(client, h, "B-REJ-EARLY", _dt(2026, 9, 5), sequence_no=1, review_status="rejected")
    tax_receipt(client, h, "B-APP-LATE", _dt(2026, 9, 10), sequence_no=2, review_status="approved")
    add_coverage_span(client, h, [_dt(2026, 9, 1), _dt(2026, 12, 31)])
    run = run_analysis(client, h, _dt(2026, 12, 15))
    f = finding(run, ids[0])
    assert f["approved_at"] == _dt(2026, 9, 10).isoformat()
    assert f["review_batch_no"] == "B-APP-LATE"

    # a yet-later rejection withdraws the approval
    tax_receipt(client, h, "B-REJ-LATER", _dt(2026, 9, 20), sequence_no=3, review_status="rejected")
    run2 = run_analysis(client, h, _dt(2026, 12, 15))
    f2 = finding(run2, ids[0])
    assert f2["approved_at"] is None
    assert f2["revocation_status"] == "superseded_by_rejection"
    assert f2["credibility_tier"] == "data_pending"


# --------------------------------------------------------------------------------------
# 12. Authorization and OpenAPI surface
# --------------------------------------------------------------------------------------

def test_timeliness_authorization_and_openapi(client, bootstrap):
    h, _ = _base(client, bootstrap)
    outsider = bootstrap["outsider"]
    body = {"entity_code": "ENT-A", "observation_cutoff": _dt(2026, 12, 15).isoformat()}
    r = client.post("/api/v1/timeliness/runs", json=body, headers=outsider)
    assert r.status_code == 403
    r = client.post("/api/v1/timeliness/runs", json=body)
    assert r.status_code == 401

    schema = client.get("/openapi.json").json()
    assert "/api/v1/timeliness/runs" in schema["paths"]
    op_tags = {
        tag
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in ("get", "post", "put", "delete", "patch")
        for tag in operation.get("tags", [])
    }
    assert "timeliness" in op_tags

    # viewer can read but not create runs
    r = client.post(
        "/api/v1/admin/users",
        json={"name": "viewer", "role": "viewer", "api_key": "viewer-key-1",
              "entity_codes": ["ENT-A"]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/timeliness/runs", json=body, headers={"X-API-Key": "viewer-key-1"})
    assert r.status_code == 403
    r = client.get("/api/v1/timeliness/coverage?entity_code=ENT-A",
                   headers={"X-API-Key": "viewer-key-1"})
    assert r.status_code == 200
