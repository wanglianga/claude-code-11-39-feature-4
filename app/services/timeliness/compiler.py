"""时效事实编译器 + 可信度分层器 + 右删失（Kaplan–Meier）分析。

样本三分（严格不混群）：
  * 首次到账完成样本：有效审核事件 → 主任务首次确认该出口行收到到账分摊（active 对账行）。
  * 足额到账完成样本：有效审核事件 → 累计 active 到账额首次满足出口行应到账额（申报金额）。
  * 未完成观察样本：到观察边界仍未确认到账；只记录等待时长，单独分布，绝不混入完成样本。
部分到账可进入首次样本，未足额前不得进入足额样本。

右删失：按主体 × 币种 × 审核批次分层，首次/足额分别构建风险集；部分到账只结束首次风险集。
失去银行覆盖的期间不计为持续等待——删失时点取覆盖边界而非观察截止日。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...util import jsonable, sha256_of
from . import coverage as cov
from .events import (
    pending_allocation_evidence,
    receipt_events_for_line,
    sort_line_review,
    sync_tax_events,
)

SECONDS_PER_DAY = Decimal("86400")
ZERO = Decimal("0.0000")

TIER_ANALYZABLE = "analyzable"
TIER_RESTRICTED = "restricted"
TIER_DATA_PENDING = "data_pending"
TIER_CONFLICT = "event_conflict"

CLASS_FIRST = "first_complete"
CLASS_FULL = "full_complete"
CLASS_CENSORED = "unfinished_censored"
CLASS_EXCLUDED = "excluded_not_sampled"


def _days(t1: datetime, t0: datetime) -> Decimal:
    delta = Decimal(str((t1 - t0).total_seconds())) / SECONDS_PER_DAY
    return delta.quantize(Decimal("0.0001"))


@dataclass
class LineSample:
    export_line_id: int
    entity_id: int
    currency: str
    export_date: datetime | None
    review_batch_no: str | None
    approved_at: datetime | None
    credibility_tier: str
    sample_classification: str
    in_first: bool = False
    in_full: bool = False
    first_at: datetime | None = None
    first_days: Decimal | None = None
    full_at: datetime | None = None
    full_days: Decimal | None = None
    waiting_days: Decimal | None = None
    boundary_at: datetime | None = None
    boundary_basis: str | None = None
    pending_evidence_exists: bool = False
    bank_uncovered: bool = False
    revocation_status: str | None = None
    same_day_ambiguity: dict = field(default_factory=dict)
    limit_reasons: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def _observation_boundary(db: Session, entity_id: int, currency: str, cutoff: datetime,
                          approved_at: datetime) -> tuple[datetime | None, str, bool]:
    """观察边界 = min(观察截止日, 完整银行覆盖最后时点)，且必须严格晚于审核事件。

    返回 (boundary, basis, bank_uncovered)。批准日晚于/等于覆盖末日 → 银行未覆盖，
    既不计等待也不计到账延迟（规格：边界只能取审核事件之后、覆盖已完成的最后时点）。
    """
    horizon = cov.covered_horizon(db, entity_id, currency)
    status_at_approval = cov.coverage_status_at(db, entity_id, currency, approved_at)
    if horizon is None or status_at_approval == cov.UNCOVERED or horizon <= approved_at:
        return None, "bank_coverage_unavailable", True
    if horizon <= cutoff:
        return horizon, "bank_coverage_horizon", False
    return cutoff, "observation_cutoff", False


def _confirmed_receipt_events(db: Session, export_line_id: int, boundary: datetime) -> list[dict]:
    """主任务确认的到账分摊事件（仅 active），按确认时刻排序，截止边界内。"""
    rows = receipt_events_for_line(db, export_line_id)
    out = []
    for r in rows:
        if r["status"] != "active":
            continue
        if r["confirmed_at"] is not None and r["confirmed_at"] <= boundary:
            out.append(r)
    out.sort(key=lambda r: (r["confirmed_at"], r["reconciliation_line_id"]))
    return out


def _frozen_line_present(db: Session, export_line_id: int) -> bool:
    return bool(
        db.scalar(
            select(m.ReconciliationLine.id)
            .where(
                m.ReconciliationLine.export_line_id == export_line_id,
                m.ReconciliationLine.status == "needs_review",
            )
            .limit(1)
        )
    )


def _same_day_dual_boundary(events: list[dict], boundary: datetime, approved_at: datetime) -> dict:
    """到账与观察截止同日且缺少时刻精度时，列出两种边界结果。"""
    ambiguous = {}
    for ev in events:
        at = ev["confirmed_at"]
        date_only_precision = (at.hour, at.minute, at.second, at.microsecond) == (0, 0, 0, 0)
        if at.date() == boundary.date() and date_only_precision:
            end_of_day = at.replace(hour=23, minute=59, second=59)
            ambiguous = {
                "event_confirmed_at": at,
                "boundary_at": boundary,
                "rule": "date-only event timestamp on the cutoff date",
                "as_recorded": {
                    "ordering": "event instant precedes boundary instant",
                    "receipt_counted": True,
                    "duration_days": str(max(ZERO, _days(at, approved_at))),
                },
                "conservative_end_of_day": {
                    "ordering": "event treated as 23:59:59; if boundary is earlier it is censored",
                    "receipt_counted": end_of_day <= boundary,
                    "censored_waiting_days": str(_days(boundary, approved_at)) if end_of_day > boundary else None,
                },
            }
    return ambiguous


def _classify_line(db: Session, export: m.ExportLine, cutoff: datetime) -> LineSample:
    sample = LineSample(
        export_line_id=export.id,
        entity_id=export.entity_id,
        currency=export.currency,
        export_date=export.export_date,
        review_batch_no=None,
        approved_at=None,
        credibility_tier=TIER_DATA_PENDING,
        sample_classification=CLASS_EXCLUDED,
    )
    conclusion = sort_line_review(db, export.id, cutoff)
    sample.revocation_status = conclusion.revocation_status
    sample.detail["review_event_sequence"] = conclusion.events
    sample.detail["late_events"] = conclusion.late_events

    # snapshot the main task's versioned declaration state at run time so the immutable run
    # keeps the submitted/reviewed/rejected/correction-pending trail it actually saw
    ledger = db.scalar(
        select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export.id)
    )
    if ledger is not None:
        from ..ledgers import declaration_history

        sample.detail["declaration_status_snapshot"] = {
            "state": ledger.state,
            "submitted_at": ledger.submitted_at,
            "reviewed_at": ledger.reviewed_at,
            "review_ref": ledger.review_ref,
            "correction_open": ledger.correction_open,
            "version": ledger.version,
            "history": declaration_history(db, export.id),
        }

    if conclusion.frozen:
        sample.credibility_tier = TIER_CONFLICT
        sample.limit_reasons.extend(conclusion.conflict_reasons)
        return sample

    if conclusion.approved_at is None:
        sample.credibility_tier = TIER_DATA_PENDING
        sample.limit_reasons.append(
            "no currently valid approval event within the observation boundary "
            f"(revocation_status={conclusion.revocation_status})"
        )
        return sample

    sample.approved_at = conclusion.approved_at
    sample.review_batch_no = conclusion.review_batch_no
    if conclusion.revocation_status == "effective_after_cutoff_kept":
        sample.limit_reasons.append(
            "a revocation notice exists but takes effect only after this observation cutoff and is "
            "not retroactive: the historical approval sample is retained for this run"
        )

    # a customs correction re-opened the declaration: the prior approval stays on record but the
    # line is only eligible for restricted analysis until the correction cycle closes
    declaration = db.scalar(
        select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export.id)
    )
    if declaration is not None and (declaration.correction_open or declaration.state == "correction_pending"):
        sample.limit_reasons.append(
            "declaration is in correction_pending after a customs correction: review basis under "
            "re-adjudication, timeliness result published as restricted analysis"
        )

    boundary, basis, uncovered = _observation_boundary(
        db, export.entity_id, export.currency, cutoff, conclusion.approved_at
    )
    sample.boundary_at = boundary
    sample.boundary_basis = basis
    sample.bank_uncovered = uncovered
    if uncovered:
        sample.credibility_tier = TIER_DATA_PENDING
        sample.limit_reasons.append(
            "bank data does not cover the post-approval period: not counted as waiting or receipt delay"
        )
        return sample

    events = _confirmed_receipt_events(db, export.id, boundary)
    # subject/currency matching: confirmed receipts must share the export line currency
    currency_mismatch = [e for e in events if e["currency"] != export.currency]
    if currency_mismatch:
        sample.limit_reasons.append(
            f"{len(currency_mismatch)} confirmed receipt event(s) in a mismatched currency excluded"
        )
        events = [e for e in events if e["currency"] == export.currency]

    frozen = _frozen_line_present(db, export.id)
    if frozen:
        sample.limit_reasons.append(
            "confirmed reconciliation line under review: occupancy frozen and excluded from receipt totals"
        )

    pending = pending_allocation_evidence(db, export)
    sample.detail["pending_allocation_flows"] = pending
    valid_pending = [p for p in pending if p.get("evidence_exists")]
    sample.pending_evidence_exists = bool(valid_pending)
    if valid_pending:
        sample.limit_reasons.append(
            "receipt evidence exists but line-level attribution is pending (candidate basis on file); "
            "pending-allocation flows are never counted in any receipt sample"
        )

    cumulative = Decimal("0")
    first_at = None
    full_at = None
    expected = export.declared_amount
    for ev in events:
        cumulative += Decimal(str(ev["amount"]))
        if first_at is None:
            first_at = ev["confirmed_at"]
        if full_at is None and cumulative >= expected:
            full_at = ev["confirmed_at"]

    sample.same_day_ambiguity = _same_day_dual_boundary(events, boundary, conclusion.approved_at)

    if first_at is not None:
        sample.in_first = True
        sample.first_at = first_at
        sample.first_days = max(ZERO, _days(first_at, conclusion.approved_at))
    if full_at is not None:
        sample.in_full = True
        sample.full_at = full_at
        sample.full_days = max(ZERO, _days(full_at, conclusion.approved_at))

    # classification — unfinished observations never join the completed-sample distributions
    if full_at is not None:
        sample.sample_classification = CLASS_FULL
    elif first_at is not None:
        sample.sample_classification = CLASS_FIRST
    else:
        sample.sample_classification = CLASS_CENSORED
        sample.waiting_days = _days(boundary, conclusion.approved_at)

    # credibility tier
    if sample.limit_reasons or sample.same_day_ambiguity or conclusion.late_events:
        sample.credibility_tier = TIER_RESTRICTED
    else:
        sample.credibility_tier = TIER_ANALYZABLE

    sample.detail["receipt_events_used"] = [
        {
            "reconciliation_line_id": e["reconciliation_line_id"],
            "bank_transaction_id": e["bank_transaction_id"],
            "txn_ref": e.get("txn_ref"),
            "amount": str(e["amount"]),
            "confirmed_at": e["confirmed_at"],
            "bank_value_date": e.get("bank_value_date"),
            "strategy": e["strategy"],
        }
        for e in events
    ]
    sample.detail["cumulative_confirmed_at_boundary"] = str(cumulative)
    sample.detail["expected_receipt_amount"] = str(expected)
    return sample


# --------------------------------------------------------------------------------------
# Kaplan–Meier with right censoring, separate risk sets per metric
# --------------------------------------------------------------------------------------

def _greenwood_ci(survival: Decimal, points: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    variance_sum = Decimal("0")
    for p in points:
        n, d = Decimal(str(p["n_risk"])), Decimal(str(p["d_events"]))
        if n > d and d > 0:
            variance_sum += d / (n * (n - d))
    se = survival * Decimal(str(math.sqrt(float(variance_sum)))) if variance_sum > 0 else Decimal("0")
    if se == 0:
        return None, None
    lower = max(Decimal("0"), survival - Decimal("1.96") * se)
    upper = min(Decimal("1"), survival + Decimal("1.96") * se)
    return lower.quantize(Decimal("0.000001")), upper.quantize(Decimal("0.000001"))


def kaplan_meier(subjects: list[LineSample], metric: str) -> dict:
    """Build the risk set from actual line events.

    metric='first_receipt': exit event = first receipt; partial receipt ends this risk set.
    metric='full_receipt':  exit event = full receipt only; partial receivers stay at risk and
                            are censored at their observation boundary.
    """
    eligible = [s for s in subjects if s.approved_at is not None and s.boundary_at is not None]
    records = []  # (t_days, event: bool)
    for s in eligible:
        censored_days = s.waiting_days or _days(s.boundary_at, s.approved_at)
        if metric == "first_receipt":
            if s.in_first and s.first_days is not None:
                records.append((s.first_days, True, s.export_line_id))
            else:
                records.append((censored_days, False, s.export_line_id))
        else:
            if s.in_full and s.full_days is not None:
                records.append((s.full_days, True, s.export_line_id))
            else:
                # censored at boundary — includes partial receivers still waiting for the remainder
                records.append((censored_days, False, s.export_line_id))

    total_n = len(records)
    event_times = sorted({t for t, ev, _ in records if ev})
    points: list[dict] = []
    survival = Decimal("1.000000")
    cumulative_events = 0
    for t in event_times:
        # risk set: subjects whose observed time (event or censoring) reaches t
        n_risk = sum(1 for tt, _, _ in records if tt >= t)
        d = sum(1 for tt, ev, _ in records if ev and tt == t)
        c = sum(1 for tt, ev, _ in records if (not ev) and tt == t)
        if n_risk == 0:
            continue
        survival = (survival * (Decimal(1) - Decimal(str(d)) / Decimal(str(n_risk)))).quantize(
            Decimal("0.000001")
        )
        cumulative_events += d
        lower, upper = _greenwood_ci(survival, points + [{"n_risk": n_risk, "d_events": d}])
        points.append(
            {
                "t_days": str(t),
                "n_risk": n_risk,
                "d_events": d,
                "c_censored_at_time": c,
                "survival": str(survival),
                "cumulative_receipt_proportion": str((Decimal(1) - survival).quantize(Decimal("0.000001"))),
                "ci_lower": str(lower) if lower is not None else None,
                "ci_upper": str(upper) if upper is not None else None,
                "cumulative_events": cumulative_events,
            }
        )

    total_events = sum(1 for _, ev, _ in records if ev)
    total_censored = total_n - total_events
    median_days = None
    median_note = None
    if total_events == 0:
        median_note = "no receipt events observed: zero-day result is never fabricated; median suppressed"
    elif not any(Decimal(p["survival"]) <= Decimal("0.5") for p in points):
        median_note = "survival never crosses 0.5 within the observation window: median not yet reachable"
    else:
        first_cross = next(p for p in points if Decimal(p["survival"]) <= Decimal("0.5"))
        median_days = first_cross["t_days"]

    return {
        "metric": metric,
        "risk_set_size": total_n,
        "events": total_events,
        "censored": total_censored,
        "points": points,
        "median_days": median_days,
        "median_note": median_note,
    }


def _distribution(values: list[Decimal]) -> dict:
    if not values:
        return {"count": 0, "min_days": None, "max_days": None, "mean_days": None, "median_days": None,
                "p25_days": None, "p75_days": None, "values_days": []}
    ordered = sorted(values)

    def quantile(q: Decimal) -> Decimal:
        if len(ordered) == 1:
            return ordered[0]
        pos = q * (len(ordered) - 1)
        lo = int(math.floor(float(pos)))
        hi = int(math.ceil(float(pos)))
        frac = pos - Decimal(lo)
        return (ordered[lo] + (ordered[hi] - ordered[lo]) * frac).quantize(Decimal("0.0001"))

    mean = (sum(ordered, Decimal("0")) / Decimal(len(ordered))).quantize(Decimal("0.0001"))
    return {
        "count": len(ordered),
        "min_days": str(ordered[0]),
        "max_days": str(ordered[-1]),
        "mean_days": str(mean),
        "median_days": str(quantile(Decimal("0.5"))),
        "p25_days": str(quantile(Decimal("0.25"))),
        "p75_days": str(quantile(Decimal("0.75"))),
        "values_days": [str(v) for v in ordered],
    }


def _stratum_key(s: LineSample, ent_code: str) -> tuple:
    return ent_code, s.currency, s.review_batch_no or "UNBATCHED", str(s.export_date.date()) if s.export_date else None


def build_run(db: Session, *, entity_id: int | None, actor: str, cutoff: datetime,
              group_filter: dict, mature_horizon_days: int) -> m.TimelinessRun:
    if entity_id is not None:
        entities = [db.get(m.Entity, entity_id)]
    else:
        entities = list(db.scalars(select(m.Entity).order_by(m.Entity.id)).all())
    entities = [e for e in entities if e is not None]

    # 1. evidence registry: sync main-task tax receipts before reading anything
    for ent in entities:
        sync_tax_events(db, ent.id)
    db.flush()

    # Refresh late flags relative to THIS run's cutoff and every prior run's cutoff.
    # A receipt issued before a (possibly earlier) observation cutoff but received afterwards is
    # a late event: the new run may incorporate it with an explicit flag, while the historical
    # run keeps its own boundary. Late-ness is a historical fact and stays sticky once set.
    prior_cutoffs = list(db.scalars(select(m.TimelinessRun.observation_cutoff)).all())
    scoped_event_stmt = select(m.TimelinessEventRecord)
    if entity_id is not None:
        scoped_event_stmt = scoped_event_stmt.where(m.TimelinessEventRecord.entity_id == entity_id)
    for record in db.scalars(scoped_event_stmt).all():
        late_now = record.issued_at <= cutoff < record.received_at
        late_vs_history = any(
            record.issued_at <= past_cutoff < record.received_at for past_cutoff in prior_cutoffs
        )
        record.late_flag = bool(record.late_flag or late_now or late_vs_history)
    db.flush()

    line_filters = group_filter.get("export_line_ids") or []
    batch_filters = set(group_filter.get("review_batch_nos") or [])
    currency_filters = set(group_filter.get("currencies") or [])

    samples: list[LineSample] = []
    for ent in entities:
        stmt = select(m.ExportLine).where(m.ExportLine.entity_id == ent.id).order_by(m.ExportLine.id)
        exports = list(db.scalars(stmt).all())
        for export in exports:
            if line_filters and export.id not in line_filters:
                continue
            if currency_filters and export.currency not in currency_filters:
                continue
            sample = _classify_line(db, export, cutoff)
            if batch_filters and sample.review_batch_no not in batch_filters:
                # declaration group filter: only lines whose valid approval belongs to a selected
                # review batch participate; unapproved/conflicted lines stay out of the group run
                continue
            samples.append(sample)

    # 2. fingerprint over the actual evidence in scope (late evidence changes it; old runs stay)
    fingerprint_src = {
        "cutoff": cutoff.isoformat(),
        "filter": group_filter,
        "events": [
            [r.id, r.sha256, r.source_status, r.late_flag]
            for r in db.scalars(select(m.TimelinessEventRecord).order_by(m.TimelinessEventRecord.id)).all()
            if entity_id is None or r.entity_id == entity_id
        ],
        "coverage": [
            [r.id, r.evidence_hash, r.status, r.is_current]
            for r in db.scalars(select(m.BankCoverageEntry).order_by(m.BankCoverageEntry.id)).all()
            if entity_id is None or r.entity_id == entity_id
        ],
        "lines": [s.export_line_id for s in samples],
    }
    fingerprint = hashlib.sha256(sha256_of(fingerprint_src).encode()).hexdigest()

    run = m.TimelinessRun(
        entity_id=entity_id,
        run_by=actor,
        observation_cutoff=cutoff,
        group_filter=jsonable(group_filter),
        mature_horizon_days=mature_horizon_days,
        input_fingerprint=fingerprint,
    )
    db.add(run)
    db.flush()

    # 3. persist line findings
    ent_codes = {e.id: e.code for e in entities}
    for s in samples:
        db.add(
            m.TimelinessLineFinding(
                run_id=run.id,
                entity_id=s.entity_id,
                export_line_id=s.export_line_id,
                review_batch_no=s.review_batch_no,
                approved_at=s.approved_at,
                credibility_tier=s.credibility_tier,
                sample_classification=s.sample_classification,
                in_first_receipt_sample=s.in_first,
                in_full_receipt_sample=s.in_full,
                first_receipt_at=s.first_at,
                first_receipt_days=s.first_days,
                full_receipt_at=s.full_at,
                full_receipt_days=s.full_days,
                waiting_days=s.waiting_days,
                observation_boundary_at=s.boundary_at,
                boundary_basis=s.boundary_basis,
                pending_evidence_exists=s.pending_evidence_exists,
                bank_uncovered=s.bank_uncovered,
                revocation_status=s.revocation_status,
                same_day_ambiguity=jsonable(s.same_day_ambiguity),
                limit_reasons=jsonable(s.limit_reasons),
                detail=jsonable(s.detail),
            )
        )

    # 4. strata: entity × currency × review batch; KM separately for first / full receipt
    strata: dict[tuple, list[LineSample]] = {}
    for s in samples:
        if s.approved_at is None or s.boundary_at is None:
            continue
        key = (ent_codes[s.entity_id], s.currency, s.review_batch_no or "UNBATCHED")
        strata.setdefault(key, []).append(s)

    stratum_rows = []
    for key, members in sorted(strata.items()):
        ent_code, currency, batch_no = key
        first_km = kaplan_meier(members, "first_receipt")
        full_km = kaplan_meier(members, "full_receipt")
        for km in (first_km, full_km):
            for p in km["points"]:
                db.add(
                    m.TimelinessSurvivalPoint(
                        run_id=run.id,
                        metric=km["metric"],
                        entity_code=ent_code,
                        currency=currency,
                        review_batch_no=batch_no,
                        t_days=Decimal(p["t_days"]),
                        n_risk=p["n_risk"],
                        d_events=p["d_events"],
                        c_censored=p["c_censored_at_time"],
                        survival=Decimal(p["survival"]),
                        ci_lower=Decimal(p["ci_lower"]) if p["ci_lower"] else None,
                        ci_upper=Decimal(p["ci_upper"]) if p["ci_upper"] else None,
                        cumulative_events=p["cumulative_events"],
                    )
                )

        completed_first = [s for s in members if s.in_first]
        completed_full = [s for s in members if s.in_full]
        censored = [s for s in members if s.sample_classification == CLASS_CENSORED]
        # simple proportions for mature batches: "finished" means the full amount arrived;
        # partial receivers and censored lines both count as unfinished
        unfinished = [s for s in members if not s.in_full]
        latest_approval = max(s.approved_at for s in members)
        mature = (cutoff - latest_approval).days >= mature_horizon_days
        row = {
            "entity_code": ent_code,
            "currency": currency,
            "review_batch_no": batch_no,
            "mature_batch": mature,
            "sample_size": len(members),
            "completed_first_count": len(completed_first),
            "completed_full_count": len(completed_full),
            "unfinished_observation_count": len(censored),
            "mature_completed_proportion": (
                str((Decimal(len(completed_full)) / Decimal(len(members))).quantize(Decimal("0.0001")))
                if mature and members
                else None
            ),
            "mature_unfinished_proportion": (
                str((Decimal(len(unfinished)) / Decimal(len(members))).quantize(Decimal("0.0001")))
                if mature and members
                else None
            ),
            "first_receipt_distribution": _distribution([s.first_days for s in completed_first]),
            "full_receipt_distribution": _distribution([s.full_days for s in completed_full]),
            "waiting_time_distribution": _distribution([s.waiting_days for s in censored]),
            "kaplan_meier": {"first_receipt": first_km, "full_receipt": full_km},
            "limit_reasons": sorted({r for s in members for r in s.limit_reasons}),
        }
        stratum_rows.append(row)

    # 5. group view by export date / entity / currency / review batch
    groups: dict[tuple, dict] = {}
    group_members: dict[tuple, list[LineSample]] = {}
    for s in samples:
        key = _stratum_key(s, ent_codes[s.entity_id])
        group_members.setdefault(key, []).append(s)
        g = groups.setdefault(
            key,
            {
                "entity_code": key[0],
                "currency": key[1],
                "review_batch_no": key[2],
                "export_date": key[3],
                "completed_sample_count": 0,
                "unfinished_observation_sample_count": 0,
                "excluded_count": 0,
                "tiers": {TIER_ANALYZABLE: 0, TIER_RESTRICTED: 0, TIER_DATA_PENDING: 0, TIER_CONFLICT: 0},
                "limit_reasons": set(),
            },
        )
        g["tiers"][s.credibility_tier] += 1
        if s.sample_classification in (CLASS_FIRST, CLASS_FULL):
            g["completed_sample_count"] += 1
        elif s.sample_classification == CLASS_CENSORED:
            g["unfinished_observation_sample_count"] += 1
        else:
            g["excluded_count"] += 1
        g["limit_reasons"].update(s.limit_reasons)
    group_rows = []
    for g in groups.values():
        members = group_members[tuple(
            [g["entity_code"], g["currency"], g["review_batch_no"], g["export_date"]]
        )]
        g["limit_reasons"] = sorted(g["limit_reasons"])
        g["first_receipt_distribution"] = _distribution(
            [s.first_days for s in members if s.in_first]
        )
        g["full_receipt_distribution"] = _distribution(
            [s.full_days for s in members if s.in_full]
        )
        g["waiting_time_distribution"] = _distribution(
            [s.waiting_days for s in members if s.sample_classification == CLASS_CENSORED]
        )
        group_rows.append(g)

    result = {
        "observation_cutoff": cutoff,
        "group_filter": jsonable(group_filter),
        "input_fingerprint": fingerprint,
        "immutability_note": "runs are append-only snapshots; late evidence creates a new run and "
        "never rewrites prior runs",
        "tier_counts": {
            tier: sum(1 for s in samples if s.credibility_tier == tier)
            for tier in (TIER_ANALYZABLE, TIER_RESTRICTED, TIER_DATA_PENDING, TIER_CONFLICT)
        },
        "sample_counts": {
            "first_receipt_completed": sum(1 for s in samples if s.in_first),
            "full_receipt_completed": sum(1 for s in samples if s.in_full),
            "unfinished_observation": sum(1 for s in samples if s.sample_classification == CLASS_CENSORED),
            "excluded_not_sampled": sum(1 for s in samples if s.sample_classification == CLASS_EXCLUDED),
        },
        "line_findings": [_finding_view(s, ent_codes[s.entity_id]) for s in samples],
        "strata": stratum_rows,
        "groups": sorted(group_rows, key=lambda g: (g["entity_code"], g["currency"], g["review_batch_no"],
                                                    g["export_date"] or "")),
        "scope_note": "timeliness and credibility facts only: no cash prediction, no transaction "
        "allocation, no declaration closure",
    }
    run.result = jsonable(result)
    db.flush()
    return run


def _case_flags(
    *,
    in_first: bool,
    in_full: bool,
    classification: str,
    late_events: list,
    bank_uncovered: bool,
    revocation_status: str | None,
    credibility_tier: str,
    pending_evidence_exists: bool,
) -> dict:
    """The eight distinguishable cases the drilldown must be able to separate."""
    return {
        "first_receipt": bool(in_first),                       # 首次到账
        "full_receipt": bool(in_full),                         # 足额到账
        "unfinished_waiting": classification == CLASS_CENSORED,  # 未完成等待
        "late_event": bool(late_events),                       # 迟到事件
        "bank_uncovered": bool(bank_uncovered),                # 银行未覆盖
        "receipt_revocation": revocation_status not in (None, "none", "superseded_by_rejection"),
        "event_conflict": credibility_tier == TIER_CONFLICT,   # 事件冲突
        "pending_allocation_receipt": bool(pending_evidence_exists),  # 待分摊到账（有行级依据）
    }


def _finding_view(s: LineSample, entity_code: str) -> dict:
    return {
        "export_line_id": s.export_line_id,
        "entity_code": entity_code,
        "currency": s.currency,
        "export_date": s.export_date,
        "review_batch_no": s.review_batch_no,
        "approved_at": s.approved_at,
        "credibility_tier": s.credibility_tier,
        "sample_classification": s.sample_classification,
        "in_first_receipt_sample": s.in_first,
        "in_full_receipt_sample": s.in_full,
        "first_receipt_at": s.first_at,
        "first_receipt_days": s.first_days,
        "full_receipt_at": s.full_at,
        "full_receipt_days": s.full_days,
        "waiting_days": s.waiting_days,
        "observation_boundary_at": s.boundary_at,
        "boundary_basis": s.boundary_basis,
        "pending_evidence_exists": s.pending_evidence_exists,
        "bank_uncovered": s.bank_uncovered,
        "revocation_status": s.revocation_status,
        "same_day_ambiguity": s.same_day_ambiguity,
        "case_flags": _case_flags(
            in_first=s.in_first,
            in_full=s.in_full,
            classification=s.sample_classification,
            late_events=s.detail.get("late_events", []),
            bank_uncovered=s.bank_uncovered,
            revocation_status=s.revocation_status,
            credibility_tier=s.credibility_tier,
            pending_evidence_exists=s.pending_evidence_exists,
        ),
        "limit_reasons": s.limit_reasons,
        "detail": s.detail,
    }


def line_drilldown(db: Session, run: m.TimelinessRun, export_line_id: int) -> dict:
    """单条记录回查：回执序列、银行覆盖边界、主任务到账归属、候选依据、可信度、样本资格。"""
    finding = db.scalar(
        select(m.TimelinessLineFinding).where(
            m.TimelinessLineFinding.run_id == run.id,
            m.TimelinessLineFinding.export_line_id == export_line_id,
        )
    )
    if finding is None:
        from fastapi import HTTPException

        raise HTTPException(404, "export line is not part of this analysis run")
    export = db.get(m.ExportLine, export_line_id)
    coverage = cov.coverage_snapshot(db, finding.entity_id, export.currency)
    horizon = cov.covered_horizon(db, finding.entity_id, export.currency)
    attribution = receipt_events_for_line(db, export_line_id)
    pending = pending_allocation_evidence(db, export)
    # main task's versioned declaration status. Prefer the snapshot frozen inside this run so
    # later declaration changes never rewrite what a historical analysis actually observed.
    snapshot = (finding.detail or {}).get("declaration_status_snapshot")
    if snapshot is not None:
        declaration_view = dict(snapshot)
        declaration_view["source"] = "run_snapshot"
    else:
        from ..ledgers import declaration_history

        declaration_ledger = db.scalar(
            select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export_line_id)
        )
        declaration_view = None
        if declaration_ledger is not None:
            declaration_view = {
                "source": "live_fallback",
                "state": declaration_ledger.state,
                "submitted_at": declaration_ledger.submitted_at,
                "reviewed_at": declaration_ledger.reviewed_at,
                "review_ref": declaration_ledger.review_ref,
                "correction_open": declaration_ledger.correction_open,
                "version": declaration_ledger.version,
                "history": declaration_history(db, export_line_id),
            }
    receipt_ledger_state = None
    for a in attribution:
        txn = db.get(m.BankTransaction, a["bank_transaction_id"])
        ledger = db.scalar(select(m.ReceiptLedger).where(m.ReceiptLedger.bank_transaction_id == txn.id))
        if ledger is not None:
            receipt_ledger_state = {
                "bank_transaction_id": txn.id,
                "txn_ref": txn.txn_ref,
                "state": ledger.state,
                "pending_allocation": ledger.pending_allocation,
            }
    case_flags = _case_flags(
        in_first=finding.in_first_receipt_sample,
        in_full=finding.in_full_receipt_sample,
        classification=finding.sample_classification,
        late_events=finding.detail.get("late_events", []),
        bank_uncovered=finding.bank_uncovered,
        revocation_status=finding.revocation_status,
        credibility_tier=finding.credibility_tier,
        pending_evidence_exists=finding.pending_evidence_exists,
    )
    return jsonable(
        {
            "run_id": run.id,
            "observation_cutoff": run.observation_cutoff,
            "input_fingerprint": run.input_fingerprint,
            "export_line": {
                "id": export.id,
                "declaration_no": export.declaration_no,
                "invoice_no": export.invoice_no,
                "currency": export.currency,
                "declared_amount": export.declared_amount,
                "export_date": export.export_date,
            },
            "case_flags": case_flags,
            "credibility_tier": finding.credibility_tier,
            "sample_qualification": {
                "sample_classification": finding.sample_classification,
                "in_first_receipt_sample": finding.in_first_receipt_sample,
                "in_full_receipt_sample": finding.in_full_receipt_sample,
                "first_receipt_days": finding.first_receipt_days,
                "full_receipt_days": finding.full_receipt_days,
                "waiting_days": finding.waiting_days,
                "approved_at": finding.approved_at,
                "review_batch_no": finding.review_batch_no,
            },
            "declaration_status": declaration_view,
            "review_event_sequence": finding.detail.get("review_event_sequence", []),
            "late_events": finding.detail.get("late_events", []),
            "bank_coverage": {
                "horizon": horizon,
                "observation_boundary_at": finding.observation_boundary_at,
                "boundary_basis": finding.boundary_basis,
                "bank_uncovered": finding.bank_uncovered,
                "calendar": coverage,
            },
            "receipt_attribution": {
                "confirmed_reconciliation_lines": attribution,
                "receipt_ledger_state": receipt_ledger_state,
                "pending_allocation_flows": pending,
                "rule": "pending-allocation flows require an auditable line-level candidate basis; "
                "same account/currency presence alone is never receipt evidence",
            },
            "revocation_status": finding.revocation_status,
            "same_day_ambiguity": finding.same_day_ambiguity,
            "limit_reasons": finding.limit_reasons,
            "evidence_detail": finding.detail,
        }
    )
