"""状态事件册与事件排序器。

事件册保存四类证据（税务回执、撤销通知、银行覆盖证明、提取批次）的签发时间、接收时间、
序号、哈希与来源状态。事件排序器按 *签发时间 + 可验证序号 + 撤销关系* 重建每个申报行的
审核事件序列：

* 接收更晚不等于状态更新——乱序到达但签发更早的回执按签发位置生效（迟到事件只做标记）。
* 较早的驳回不能覆盖后续批准；事件按签发位置回放得到当前有效审核结论。
* 两份回执签发时间相同、序号冲突且来源权威性无法判定时，冻结该行结论，不生成任何时长。
* 撤销通知按三分支处理：截止日后生效且不追溯→历史运行保留；明确追溯→剔除并重判；
  范围/指向不明→事件冲突或待补数据，绝不因序号更高就删除批准样本。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...util import jsonable

TAX_RECEIPT = "tax_receipt"
REVOCATION = "revocation_notice"
COVERAGE_PROOF = "bank_coverage_proof"
EXTRACTION_BATCH = "extraction_batch"


# --------------------------------------------------------------------------------------
# Evidence registry writes
# --------------------------------------------------------------------------------------

def _hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def sync_tax_events(db: Session, entity_id: int) -> list[m.TimelinessEventRecord]:
    """把主任务受理账中的税务回执同步进状态事件册（幂等）。

    接收时间取来源文档的 received_time；签发时间取税务行 issue_time；序号取可验证
    sequence_no；哈希与来源状态一并固化，供分析运行在自己的边界内重放。
    """
    rows = db.scalars(
        select(m.TaxLine)
        .where(m.TaxLine.entity_id == entity_id)
        .order_by(m.TaxLine.id)
    ).all()
    synced: list[m.TimelinessEventRecord] = []
    for tax in rows:
        doc = db.get(m.SourceDocument, tax.document_id)
        if doc is None:
            continue
        digest = _hash("tax", doc.id, tax.id, tax.issue_time.isoformat(), tax.sequence_no)
        existing = db.scalar(
            select(m.TimelinessEventRecord).where(
                m.TimelinessEventRecord.record_kind == TAX_RECEIPT,
                m.TimelinessEventRecord.sha256 == digest,
            )
        )
        if existing is not None:
            synced.append(existing)
            continue
        record = m.TimelinessEventRecord(
            entity_id=entity_id,
            record_kind=TAX_RECEIPT,
            source_document_id=doc.id,
            export_line_id=tax.export_line_id,
            batch_no=tax.batch_no,
            declaration_no=tax.declaration_no,
            review_status=tax.review_status,
            source_status="conflicted" if tax.order_conflict else "valid",
            issued_at=tax.issue_time,
            received_at=doc.received_time,
            sequence_no=tax.sequence_no,
            sha256=digest,
            payload=jsonable(
                {
                    "tax_line_id": tax.id,
                    "document_id": doc.id,
                    "document_sha256": doc.sha256,
                    "amount": tax.amount,
                    "currency": tax.currency,
                    "order_conflict": tax.order_conflict,
                    "applied_sequence": tax.applied_sequence,
                }
            ),
        )
        db.add(record)
        synced.append(record)
    db.flush()
    return synced


def register_revocation_notice(db: Session, req, entity: m.Entity) -> dict:
    """登记一条税务批准撤销通知（append-only）。

    通知必须声明它指向的批准事件（批次号 / 来源文档 / 出口行至少一项）。指向或范围无法
    解析时照常登记，排序器会把受影响行列为事件冲突或待补数据，而不是拒绝受理。
    """
    if not (req.target_batch_no or req.target_document_id or req.target_export_line_id):
        raise HTTPException(
            422,
            "revocation notice must point at an approval: target_batch_no / "
            "target_document_id / target_export_line_id is required",
        )
    received = req.received_at or req.issued_at
    effective = req.effective_at or received
    digest = req.sha256 or _hash(
        "revocation",
        entity.id,
        req.external_no,
        req.issued_at.isoformat(),
        req.sequence_no,
        req.target_batch_no,
        req.target_document_id,
        req.target_export_line_id,
    )
    if db.scalar(select(m.TimelinessEventRecord.id).where(
        m.TimelinessEventRecord.record_kind == REVOCATION,
        m.TimelinessEventRecord.sha256 == digest,
    )):
        raise HTTPException(409, "revocation notice with this evidence fingerprint already registered")

    target_line = req.target_export_line_id
    ambiguous = False
    if req.target_batch_no:
        tax = db.scalar(
            select(m.TaxLine).where(
                m.TaxLine.entity_id == entity.id,
                m.TaxLine.batch_no == req.target_batch_no,
            ).order_by(m.TaxLine.id).limit(1)
        )
        if tax is None:
            ambiguous = True
        elif target_line is None:
            target_line = tax.export_line_id
    if req.target_document_id and target_line is None:
        tax = db.scalar(
            select(m.TaxLine).where(
                m.TaxLine.entity_id == entity.id,
                m.TaxLine.document_id == req.target_document_id,
            ).order_by(m.TaxLine.id).limit(1)
        )
        if tax is not None:
            target_line = tax.export_line_id
        else:
            ambiguous = True

    record = m.TimelinessEventRecord(
        entity_id=entity.id,
        record_kind=REVOCATION,
        export_line_id=target_line,
        currency=req.currency.upper() if req.currency else None,
        batch_no=req.target_batch_no,
        declaration_no=req.declaration_no,
        review_status="revoked",
        source_status="valid" if not ambiguous else "conflicted",
        issued_at=req.issued_at,
        received_at=received,
        sequence_no=req.sequence_no,
        sha256=digest,
        target_ref=req.target_batch_no or (f"doc:{req.target_document_id}" if req.target_document_id else None),
        target_export_line_id=target_line,
        effective_at=effective,
        retroactive=req.retroactive,
        payload=jsonable(
            {
                "external_no": req.external_no,
                "original_location": req.original_location,
                "target_document_id": req.target_document_id,
                "target_ambiguous": ambiguous,
                "note": req.note,
            }
        ),
    )
    db.add(record)
    db.add(
        m.AuditLog(
            entity_id=entity.id,
            actor="timeliness",
            action="timeliness.revocation_registered",
            object_type="timeliness_event_record",
            payload=jsonable(
                {
                    "target_batch_no": req.target_batch_no,
                    "target_export_line_id": target_line,
                    "retroactive": req.retroactive,
                    "effective_at": effective,
                    "ambiguous": ambiguous,
                }
            ),
        )
    )
    db.flush()
    return {"event_record_id": record.id, "target_ambiguous": ambiguous, "sha256": digest}


# --------------------------------------------------------------------------------------
# Event sorter
# --------------------------------------------------------------------------------------

@dataclass
class SourcedEvent:
    kind: str
    issued_at: datetime
    received_at: datetime
    effective_at: datetime | None
    sequence_no: int | None
    review_status: str | None
    source_status: str
    ref: str
    record_id: int
    is_late: bool = False
    retroactive: bool = False
    target_ref: str | None = None
    target_export_line_id: int | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class LineConclusion:
    export_line_id: int
    review_batch_no: str | None
    approved_at: datetime | None = None
    frozen: bool = False
    conflict_reasons: list[str] = field(default_factory=list)
    # none | effective_after_cutoff_kept | retroactive_removed | non_retroactive_removed | ambiguous
    revocation_status: str = "none"
    events: list[dict] = field(default_factory=list)
    late_events: list[dict] = field(default_factory=list)
    has_approval_evidence: bool = False


def _line_review_records(db: Session, export_line_id: int) -> list[m.TimelinessEventRecord]:
    """All review evidence for one line, including batch-targeted revocations fanning out to it."""
    line_records = db.scalars(
        select(m.TimelinessEventRecord)
        .where(
            m.TimelinessEventRecord.export_line_id == export_line_id,
            m.TimelinessEventRecord.record_kind.in_((TAX_RECEIPT, REVOCATION)),
        )
    ).all()
    batch_nos = {
        r.batch_no
        for r in line_records
        if r.record_kind == TAX_RECEIPT and r.batch_no
    }
    batch_revocations = []
    if batch_nos:
        batch_revocations = list(
            db.scalars(
                select(m.TimelinessEventRecord)
                .where(
                    m.TimelinessEventRecord.record_kind == REVOCATION,
                    m.TimelinessEventRecord.target_ref.in_(batch_nos),
                )
            ).all()
        )
    records = {r.id: r for r in line_records}
    for r in batch_revocations:
        records.setdefault(r.id, r)
    return sorted(records.values(), key=lambda r: (r.issued_at, r.sequence_no or 10**12, r.id))


def _line_events(db: Session, export_line_id: int, cutoff: datetime) -> list[SourcedEvent]:
    """Load review evidence for one line, scoped to a run's observation cutoff.

    Inclusion rule: only evidence *issued* by the cutoff participates. A record issued before
    the cutoff but received afterwards is a late event — it is included and flagged, never
    silently back-fitted into earlier runs. Batch-targeted revocations fan out to every line
    whose tax receipts carry that batch, even when the notice itself was bound to one line.
    """
    out: list[SourcedEvent] = []
    for r in _line_review_records(db, export_line_id):
        if r.issued_at > cutoff and r.record_kind != REVOCATION:
            # verdict evidence the run could not have known at its cutoff — excluded, not guessed
            continue
        out.append(
            SourcedEvent(
                kind=r.record_kind,
                issued_at=r.issued_at,
                received_at=r.received_at,
                effective_at=r.effective_at,
                sequence_no=r.sequence_no,
                review_status=r.review_status,
                source_status=r.source_status,
                ref=r.batch_no or f"record:{r.id}",
                record_id=r.id,
                # late relative to this cutoff, or already known late relative to an earlier run
                is_late=bool(r.late_flag) or (r.issued_at <= cutoff < r.received_at),
                retroactive=r.retroactive,
                target_ref=r.target_ref,
                target_export_line_id=r.target_export_line_id,
                payload=r.payload or {},
            )
        )
    out.sort(key=lambda e: (e.issued_at, e.sequence_no if e.sequence_no is not None else 10**12, e.record_id))
    return out


def _detect_sequence_collision(events: list[SourcedEvent]) -> bool:
    """Same issue time with unresolvable ordering → freeze.

    Different verifiable sequence numbers at the same issue time *prove* an order (sequence is
    the tie-breaker), so they do not collide. A collision needs equal/missing sequences at the
    same issue time, or an upstream source_status=conflicted flag — and no authoritative source
    to break the tie (the registry carries no authority override, so such ties stay frozen).
    """
    tax = [e for e in events if e.kind == TAX_RECEIPT]
    for i in range(len(tax)):
        for j in range(i + 1, len(tax)):
            a, b = tax[i], tax[j]
            if a.issued_at != b.issued_at:
                continue
            if a.source_status == "conflicted" or b.source_status == "conflicted":
                return True
            if a.sequence_no is None or b.sequence_no is None:
                return True
            if a.sequence_no == b.sequence_no:
                return True
    return False


def sort_line_review(db: Session, export_line_id: int, cutoff: datetime) -> LineConclusion:
    """Replay the review-event sequence for one export line at a fixed observation cutoff."""
    events = _line_events(db, export_line_id, cutoff)
    # full sequence for drilldown, including evidence issued after the cutoff (marked out of scope)
    all_records = _line_review_records(db, export_line_id)
    conclusion = LineConclusion(export_line_id=export_line_id, review_batch_no=None)
    conclusion.events = [
        {
            "kind": r.record_kind,
            "issued_at": r.issued_at,
            "received_at": r.received_at,
            "effective_at": r.effective_at,
            "sequence_no": r.sequence_no,
            "review_status": r.review_status,
            "source_status": r.source_status,
            "ref": r.batch_no or f"record:{r.id}",
            "in_scope": r.issued_at <= cutoff,
            "is_late": bool(r.late_flag) or (r.issued_at <= cutoff < r.received_at),
            "retroactive": r.retroactive,
            "target_ref": r.target_ref,
        }
        for r in all_records
    ]
    conclusion.late_events = [ev for ev in conclusion.events if ev["is_late"]]

    if _detect_sequence_collision(events):
        conclusion.frozen = True
        conclusion.conflict_reasons.append(
            "two receipts share an issue time with conflicting/unverifiable sequence and no "
            "authoritative source: review conclusion frozen, no duration generated"
        )
        return conclusion

    # Replay verdict events at their issue position. A later approval overrides an earlier
    # rejection; late arrival never changes the position. Only evidence issued by the cutoff
    # participates, except late evidence (issued <= cutoff, received afterwards) which a new
    # run is allowed to incorporate — it is still flagged.
    verdict_events = [
        e
        for e in events
        if e.kind == TAX_RECEIPT and e.review_status in ("approved", "rejected")
    ]
    current_approval: SourcedEvent | None = None
    ever_approved = False
    for e in verdict_events:
        if e.review_status == "approved" and e.source_status != "conflicted":
            current_approval = e
            ever_approved = True
        elif e.review_status == "rejected":
            # rejection only withdraws an approval that was actually earlier in issue order
            if current_approval is not None and e.issued_at >= current_approval.issued_at:
                current_approval = None
    conclusion.has_approval_evidence = ever_approved

    # Revocation replay — the three spec branches.
    revocations = [e for e in events if e.kind == REVOCATION]
    for rev in revocations:
        if current_approval is None:
            continue
        points_at_approval = (
            rev.target_ref is None
            or rev.target_ref == current_approval.ref
            or rev.target_export_line_id in (None, export_line_id)
        )
        if not points_at_approval:
            continue
        if rev.source_status == "conflicted" or rev.payload.get("target_ambiguous"):
            conclusion.revocation_status = "ambiguous"
            conclusion.frozen = True
            conclusion.conflict_reasons.append(
                "revocation scope or target event cannot be determined; approval retained but "
                "the line is frozen pending supplementary data"
            )
            continue
        effective = rev.effective_at or rev.received_at
        if effective > cutoff and not rev.retroactive:
            # branch 1: takes effect only after this historical boundary — sample is kept
            conclusion.revocation_status = "effective_after_cutoff_kept"
            continue
        if rev.retroactive:
            # branch 2a: explicitly negates the approved period — re-adjudicate, drop from samples
            conclusion.revocation_status = "retroactive_removed"
            current_approval = None
            continue
        # branch 2b: effective within boundary, non-retroactive — approval no longer valid now
        conclusion.revocation_status = "non_retroactive_removed"
        current_approval = None

    if current_approval is not None:
        conclusion.approved_at = current_approval.issued_at
        conclusion.review_batch_no = current_approval.ref
    elif conclusion.revocation_status == "none" and ever_approved:
        # approval withdrawn by a later rejection: no current valid approval, but not a conflict
        conclusion.revocation_status = "superseded_by_rejection"
    return conclusion


def receipt_events_for_line(db: Session, export_line_id: int) -> list[dict]:
    """Read the main task's versioned receipt attribution state for one export line.

    Only active reconciliation lines count as confirmed receipt allocations (frozen
    needs_review lines are excluded by the main task too). Pending-allocation bank flows are
    returned separately so the compiler can require an auditable candidate basis.
    """
    lines = db.scalars(
        select(m.ReconciliationLine)
        .where(m.ReconciliationLine.export_line_id == export_line_id)
        .order_by(m.ReconciliationLine.confirmed_at, m.ReconciliationLine.id)
    ).all()
    confirmed = []
    for l in lines:
        txn = db.get(m.BankTransaction, l.bank_transaction_id)
        confirmed.append(
            {
                "reconciliation_line_id": l.id,
                "bank_transaction_id": l.bank_transaction_id,
                "amount": l.amount,
                "currency": l.currency,
                "status": l.status,
                "strategy": l.strategy,
                # the timing event per spec: the main task's confirmation of the allocation
                "confirmed_at": l.confirmed_at,
                # the underlying bank value date, retained for evidence review
                "bank_value_date": txn.value_date if txn else None,
                "txn_ref": txn.txn_ref if txn else None,
            }
        )
    return confirmed


def pending_allocation_evidence(db: Session, export: m.ExportLine) -> list[dict]:
    """Pending-allocation flows count as receipt evidence only with an auditable line-level basis.

    A pending-allocation transaction in the same account/currency alone proves nothing about
    this export line. Accepted bases: a proposed reconciliation candidate (payment note, batch
    key, contract or other auditable mapping recorded by the main task) or a parseable unique
    line-level payment reference.
    """
    txns = db.scalars(
        select(m.BankTransaction)
        .join(m.ReceiptLedger, m.ReceiptLedger.bank_transaction_id == m.BankTransaction.id)
        .where(
            m.BankTransaction.entity_id == export.entity_id,
            m.BankTransaction.currency == export.currency,
            m.ReceiptLedger.pending_allocation.is_(True),
        )
        .order_by(m.BankTransaction.id)
    ).all()
    evidence: list[dict] = []
    for txn in txns:
        candidate = db.scalar(
            select(m.ReconciliationCandidate).where(
                m.ReconciliationCandidate.bank_transaction_id == txn.id,
                m.ReconciliationCandidate.export_line_id == export.id,
                m.ReconciliationCandidate.status == "proposed",
            )
        )
        unique_basis = None
        from ..intake import parse_unique_reference

        parsed = parse_unique_reference(txn.remittance_info)
        if parsed:
            unique_basis = parsed
        if candidate is None and unique_basis is None:
            # same account/currency pending flow without a line-level basis: explicitly NOT evidence
            evidence.append(
                {
                    "bank_transaction_id": txn.id,
                    "txn_ref": txn.txn_ref,
                    "amount": txn.amount,
                    "evidence_exists": False,
                    "reason": "pending-allocation flow in same entity/currency without line-level "
                    "candidate basis or unique payment reference",
                }
            )
            continue
        evidence.append(
            {
                "bank_transaction_id": txn.id,
                "txn_ref": txn.txn_ref,
                "amount": txn.amount,
                "evidence_exists": True,
                "attribution_pending": True,
                "basis": "reconciliation_candidate" if candidate else "unique_payment_reference",
                "candidate_id": candidate.id if candidate else None,
                "candidate_reasons": candidate.reasons if candidate else None,
                "unique_reference": unique_basis,
                "remittance_info": txn.remittance_info,
            }
        )
    return evidence
