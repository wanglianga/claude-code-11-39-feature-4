"""银行数据覆盖日历与数据提取批次管理。

覆盖按 *账户 × 币种 × 日期* 登记为 covered / partial / uncovered，并随提取批次版本化：
更正缩减覆盖时旧行置为 superseded（不再被新分析读取），但引用旧覆盖的历史分析运行原样保留。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...util import jsonable

COVERED = "covered"
PARTIAL = "partial"
UNCOVERED = "uncovered"

_STATUS_RANK = {UNCOVERED: 0, PARTIAL: 1, COVERED: 2}


def _digest(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _ensure_extraction_batch_event(db, entity: m.Entity, *, batch_no: str, account: str,
                                   currency: str, issued: datetime, received: datetime,
                                   sequence_no, status: str, note) -> None:
    """One extraction-batch evidence record per batch in the status event registry."""
    digest = _digest("extraction-batch", entity.id, account, currency, batch_no, status)
    if db.scalar(
        select(m.TimelinessEventRecord.id).where(
            m.TimelinessEventRecord.record_kind == "extraction_batch",
            m.TimelinessEventRecord.sha256 == digest,
        )
    ):
        return
    db.add(
        m.TimelinessEventRecord(
            entity_id=entity.id,
            record_kind="extraction_batch",
            bank_account=account,
            currency=currency,
            batch_no=batch_no,
            review_status=None,
            source_status=status,
            issued_at=issued,
            received_at=received,
            sequence_no=sequence_no,
            sha256=digest,
            payload=jsonable({"note": note, "kind": "data_extraction_batch"}),
        )
    )


def _add_coverage_event(db, entity: m.Entity, *, account: str, currency: str, batch_no: str,
                        coverage_date: datetime, issued: datetime, received: datetime,
                        sequence_no, status: str, digest: str, note,
                        source_status: str = "valid") -> None:
    """Mirror each coverage proof into the append-only status event registry."""
    db.add(
        m.TimelinessEventRecord(
            entity_id=entity.id,
            record_kind="bank_coverage_proof",
            bank_account=account,
            currency=currency,
            batch_no=batch_no,
            review_status=None,
            source_status=source_status,
            issued_at=issued,
            received_at=received,
            sequence_no=sequence_no,
            sha256=digest,
            payload=jsonable(
                {
                    "coverage_date": coverage_date,
                    "coverage_status": status,
                    "extraction_batch_no": batch_no,
                    "note": note,
                }
            ),
        )
    )


def register_coverage_proof(db: Session, req, entity: m.Entity) -> dict:
    """登记一条银行覆盖证明（同时记录提取批次的签发/接收时间、序号、哈希与来源状态）。"""
    currency = req.currency.upper()
    issued = req.issued_at or req.coverage_date
    received = req.received_at or issued
    digest = req.evidence_hash or _digest(
        "coverage",
        entity.id,
        req.bank_account,
        currency,
        req.coverage_date.isoformat(),
        req.extraction_batch_no,
        req.status,
        req.completion,
    )
    existing = db.scalar(
        select(m.BankCoverageEntry).where(
            m.BankCoverageEntry.bank_account == req.bank_account,
            m.BankCoverageEntry.currency == currency,
            m.BankCoverageEntry.coverage_date == req.coverage_date,
            m.BankCoverageEntry.extraction_batch_no == req.extraction_batch_no,
        )
    )
    if existing is not None:
        raise HTTPException(409, "coverage slot for this account/currency/date/batch already registered")

    source_status = "valid"
    if req.extraction_status == "withdrawn":
        source_status = "withdrawn"
    elif req.extraction_status == "corrected":
        source_status = "superseded"
    entry = m.BankCoverageEntry(
        entity_id=entity.id,
        bank_account=req.bank_account,
        currency=currency,
        coverage_date=req.coverage_date,
        status=req.status,
        completion=req.completion,
        extraction_batch_no=req.extraction_batch_no,
        evidence_hash=digest,
        source_status=source_status,
        is_current=source_status == "valid",
        issued_at=issued,
        received_at=received,
        sequence_no=req.sequence_no,
        payload=jsonable({"note": req.note, "extraction_status": req.extraction_status}),
    )
    db.add(entry)
    _ensure_extraction_batch_event(
        db,
        entity,
        batch_no=req.extraction_batch_no,
        account=req.bank_account,
        currency=currency,
        issued=issued,
        received=received,
        sequence_no=req.sequence_no,
        status=source_status,
        note=req.note,
    )
    _add_coverage_event(
        db,
        entity,
        account=req.bank_account,
        currency=currency,
        batch_no=req.extraction_batch_no,
        coverage_date=req.coverage_date,
        issued=issued,
        received=received,
        sequence_no=req.sequence_no,
        status=req.status,
        digest=digest,
        note=req.note,
        source_status=source_status,
    )
    db.add(
        m.AuditLog(
            entity_id=entity.id,
            actor="timeliness",
            action="timeliness.coverage_registered",
            object_type="bank_coverage_calendar",
            payload=jsonable(
                {
                    "bank_account": req.bank_account,
                    "currency": currency,
                    "coverage_date": req.coverage_date,
                    "status": req.status,
                    "extraction_batch_no": req.extraction_batch_no,
                }
            ),
        )
    )
    db.flush()
    return {"coverage_id": entry.id, "evidence_hash": digest, "status": entry.status}


def apply_coverage_correction(db: Session, req, entity: m.Entity) -> dict:
    """提取批次更正：区间内当前覆盖行全部 superseded，写入新批次覆盖行。

    历史分析运行保存的是自己的 findings 快照，不会被回写；新运行只能读到 is_current 的新行。
    若更正把 covered 降为 partial/uncovered，受影响出口行在下一次运行中转为待补数据。
    """
    currency = req.currency.upper()
    old_rows = db.scalars(
        select(m.BankCoverageEntry)
        .where(
            m.BankCoverageEntry.entity_id == entity.id,
            m.BankCoverageEntry.bank_account == req.bank_account,
            m.BankCoverageEntry.currency == currency,
            m.BankCoverageEntry.coverage_date >= req.date_from,
            m.BankCoverageEntry.coverage_date <= req.date_to,
            m.BankCoverageEntry.is_current.is_(True),
        )
        .order_by(m.BankCoverageEntry.coverage_date)
    ).all()
    if not old_rows:
        raise HTTPException(404, "no current coverage entries in the correction window")

    corrected_at = req.corrected_at or datetime.now(req.date_from.tzinfo)
    created: list[int] = []
    reduced = []
    for old in old_rows:
        old.is_current = False
        old.source_status = "superseded"
        old.corrected_at = corrected_at
        # keep the event registry consistent: the old coverage proof is withdrawn, not deleted
        old_events = db.scalars(
            select(m.TimelinessEventRecord).where(
                m.TimelinessEventRecord.record_kind == "bank_coverage_proof",
                m.TimelinessEventRecord.sha256 == old.evidence_hash,
            )
        ).all()
        for ev in old_events:
            ev.source_status = "superseded"
        digest = _digest(
            "coverage-corrected",
            old.id,
            req.extraction_batch_no,
            req.new_status,
            req.new_completion,
            corrected_at.isoformat(),
        )
        new = m.BankCoverageEntry(
            entity_id=entity.id,
            bank_account=req.bank_account,
            currency=currency,
            coverage_date=old.coverage_date,
            status=req.new_status,
            completion=req.new_completion,
            extraction_batch_no=req.extraction_batch_no,
            evidence_hash=digest,
            source_status="valid",
            is_current=True,
            issued_at=corrected_at,
            received_at=corrected_at,
            payload=jsonable(
                {
                    "note": req.note,
                    "extraction_status": "corrected",
                    "replaces_coverage_id": old.id,
                    "previous_status": old.status,
                }
            ),
        )
        db.add(new)
        db.flush()
        old.superseded_by_id = new.id
        created.append(new.id)
        _add_coverage_event(
            db,
            entity,
            account=req.bank_account,
            currency=currency,
            batch_no=req.extraction_batch_no,
            coverage_date=old.coverage_date,
            issued=corrected_at,
            received=corrected_at,
            sequence_no=None,
            status=req.new_status,
            digest=digest,
            note=req.note or "coverage corrected by a re-issued extraction batch",
            source_status="valid",
        )
        if _STATUS_RANK[req.new_status] < _STATUS_RANK[old.status]:
            reduced.append(old.coverage_date)

    _ensure_extraction_batch_event(
        db,
        entity,
        batch_no=req.extraction_batch_no,
        account=req.bank_account,
        currency=currency,
        issued=corrected_at,
        received=corrected_at,
        sequence_no=None,
        status="superseded",
        note=req.note or "corrected extraction batch with reduced coverage",
    )

    db.add(
        m.AuditLog(
            entity_id=entity.id,
            actor="timeliness",
            action="timeliness.coverage_corrected",
            object_type="bank_coverage_calendar",
            payload=jsonable(
                {
                    "bank_account": req.bank_account,
                    "currency": currency,
                    "window": [req.date_from, req.date_to],
                    "superseded": [r.id for r in old_rows],
                    "new_rows": created,
                    "reduced_dates": reduced,
                }
            ),
        )
    )
    db.flush()
    return {
        "superseded_coverage_ids": [r.id for r in old_rows],
        "new_coverage_ids": created,
        "reduced_coverage_dates": jsonable(reduced),
    }


def _accounts_for_currency(db: Session, entity_id: int, currency: str) -> list[str]:
    rows = db.scalars(
        select(m.BankCoverageEntry.bank_account)
        .where(
            m.BankCoverageEntry.entity_id == entity_id,
            m.BankCoverageEntry.currency == currency,
            m.BankCoverageEntry.is_current.is_(True),
        )
        .distinct()
    ).all()
    return list(rows)


def covered_horizon(db: Session, entity_id: int, currency: str) -> datetime | None:
    """该主体/币种下 *完整覆盖且提取状态有效* 的最后时点（跨账户取最早的账户边界）。

    只有 status=covered 的当前行才能延伸完整覆盖边界；partial/uncovered 不行。
    没有任何覆盖行时返回 None（调用方据此标记银行未覆盖）。
    """
    horizons: list[datetime] = []
    for account in _accounts_for_currency(db, entity_id, currency):
        last = db.scalar(
            select(m.BankCoverageEntry.coverage_date)
            .where(
                m.BankCoverageEntry.entity_id == entity_id,
                m.BankCoverageEntry.bank_account == account,
                m.BankCoverageEntry.currency == currency,
                m.BankCoverageEntry.is_current.is_(True),
                m.BankCoverageEntry.status == COVERED,
                m.BankCoverageEntry.source_status == "valid",
            )
            .order_by(m.BankCoverageEntry.coverage_date.desc())
            .limit(1)
        )
        if last is not None:
            horizons.append(last)
    return min(horizons) if horizons else None


def coverage_status_at(db: Session, entity_id: int, currency: str, at: datetime) -> str:
    """某时点的覆盖状态：取该日或之前最近一条当前覆盖行；无任何行时为 uncovered。"""
    accounts = _accounts_for_currency(db, entity_id, currency)
    if not accounts:
        return UNCOVERED
    worst = COVERED
    found_any = False
    for account in accounts:
        row = db.scalar(
            select(m.BankCoverageEntry)
            .where(
                m.BankCoverageEntry.entity_id == entity_id,
                m.BankCoverageEntry.bank_account == account,
                m.BankCoverageEntry.currency == currency,
                m.BankCoverageEntry.is_current.is_(True),
                m.BankCoverageEntry.coverage_date <= at,
            )
            .order_by(m.BankCoverageEntry.coverage_date.desc())
            .limit(1)
        )
        if row is None:
            return UNCOVERED
        found_any = True
        if _STATUS_RANK[row.status] < _STATUS_RANK[worst]:
            worst = row.status
    return worst if found_any else UNCOVERED


def coverage_snapshot(db: Session, entity_id: int, currency: str) -> list[dict]:
    """当前覆盖日历（仅 is_current 行），供行级回查展示覆盖边界。"""
    rows = db.scalars(
        select(m.BankCoverageEntry)
        .where(
            m.BankCoverageEntry.entity_id == entity_id,
            m.BankCoverageEntry.currency == currency,
            m.BankCoverageEntry.is_current.is_(True),
        )
        .order_by(m.BankCoverageEntry.bank_account, m.BankCoverageEntry.coverage_date)
    ).all()
    return [
        {
            "coverage_id": r.id,
            "bank_account": r.bank_account,
            "currency": r.currency,
            "coverage_date": r.coverage_date,
            "status": r.status,
            "completion": r.completion,
            "extraction_batch_no": r.extraction_batch_no,
            "source_status": r.source_status,
            "issued_at": r.issued_at,
            "received_at": r.received_at,
            "sequence_no": r.sequence_no,
            "evidence_hash": r.evidence_hash,
        }
        for r in rows
    ]
