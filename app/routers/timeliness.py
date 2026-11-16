"""退税审核到账时效与状态可信度分析路由。

只输出时效分析事实与数据质量证书：不预测现金、不分配流水、不关闭申报行，
也不代办申报、退税、付款或资金操作。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import Principal, get_principal, require_writer
from ..db import get_db
from ..schemas import (
    CoverageCorrectionRequest,
    CoverageProofRequest,
    RevocationNoticeRequest,
    TimelinessRunRequest,
)
from ..services.timeliness import compiler, coverage, events
from ..util import jsonable

router = APIRouter(prefix="/api/v1/timeliness", tags=["timeliness"])


def _entity(db: Session, code: str) -> m.Entity:
    ent = db.scalar(select(m.Entity).where(m.Entity.code == code))
    if ent is None:
        raise HTTPException(404, f"entity not found: {code}")
    return ent


def _load_run(db: Session, run_id: int, principal: Principal) -> m.TimelinessRun:
    run = db.get(m.TimelinessRun, run_id)
    if run is None:
        raise HTTPException(404, "timeliness run not found")
    if run.entity_id is not None:
        principal.require_entity(run.entity_id)
    return run


# --------------------------------------------------------------------------------------
# Evidence registry: revocation notices and bank coverage
# --------------------------------------------------------------------------------------

@router.post("/revocations", status_code=201)
def register_revocation(
    body: RevocationNoticeRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记税务批准撤销通知（append-only），记录签发/接收时间、序号、哈希、生效时间与追溯范围。

    指向或范围不明的通知照常受理，事件排序器会把受影响行列为事件冲突/待补数据，
    不会仅因序号更高就删除批准样本。
    """
    ent = _entity(db, body.entity_code)
    principal.require_entity(ent.id)
    out = events.register_revocation_notice(db, body, ent)
    db.commit()
    return out


@router.post("/coverage", status_code=201)
def register_coverage(
    body: CoverageProofRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """登记银行数据覆盖证明与数据提取批次（账户 × 币种 × 日期，covered/partial/uncovered）。"""
    ent = _entity(db, body.entity_code)
    principal.require_entity(ent.id)
    out = coverage.register_coverage_proof(db, body, ent)
    db.commit()
    return out


@router.post("/coverage/corrections", status_code=201)
def correct_coverage(
    body: CoverageCorrectionRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """提取批次更正：区间内旧覆盖置为 superseded 并写入新覆盖。

    已引用旧覆盖的历史分析运行保留原版本；新分析不得继续使用旧覆盖，受影响行转为待补数据。
    """
    ent = _entity(db, body.entity_code)
    principal.require_entity(ent.id)
    out = coverage.apply_coverage_correction(db, body, ent)
    db.commit()
    return out


@router.get("/coverage")
def list_coverage(
    entity_code: str,
    currency: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """查看当前生效的银行覆盖日历（仅 is_current 行；被更正替换的历史行不在其中）。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    stmt = (
        select(m.BankCoverageEntry)
        .where(
            m.BankCoverageEntry.entity_id == ent.id,
            m.BankCoverageEntry.is_current.is_(True),
        )
        .order_by(m.BankCoverageEntry.bank_account, m.BankCoverageEntry.currency,
                  m.BankCoverageEntry.coverage_date)
    )
    if currency:
        stmt = stmt.where(m.BankCoverageEntry.currency == currency.upper())
    rows = db.scalars(stmt).all()
    return [
        jsonable(
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
        )
        for r in rows
    ]


@router.get("/event-records")
def list_event_records(
    entity_code: str,
    record_kind: str | None = Query(default=None),
    export_line_id: int | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """查看状态事件册（税务回执/撤销通知/覆盖证明/提取批次）及其签发/接收时间、序号、哈希、来源状态。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    stmt = (
        select(m.TimelinessEventRecord)
        .where(m.TimelinessEventRecord.entity_id == ent.id)
        .order_by(m.TimelinessEventRecord.issued_at, m.TimelinessEventRecord.id)
    )
    if record_kind:
        stmt = stmt.where(m.TimelinessEventRecord.record_kind == record_kind)
    if export_line_id is not None:
        stmt = stmt.where(m.TimelinessEventRecord.export_line_id == export_line_id)
    rows = db.scalars(stmt.limit(500)).all()
    return [
        jsonable(
            {
                "id": r.id,
                "record_kind": r.record_kind,
                "source_document_id": r.source_document_id,
                "export_line_id": r.export_line_id,
                "bank_account": r.bank_account,
                "currency": r.currency,
                "batch_no": r.batch_no,
                "declaration_no": r.declaration_no,
                "review_status": r.review_status,
                "source_status": r.source_status,
                "issued_at": r.issued_at,
                "received_at": r.received_at,
                "sequence_no": r.sequence_no,
                "sha256": r.sha256,
                "target_ref": r.target_ref,
                "target_export_line_id": r.target_export_line_id,
                "effective_at": r.effective_at,
                "retroactive": r.retroactive,
                "late_flag": r.late_flag,
                "payload": r.payload,
            }
        )
        for r in rows
    ]


# --------------------------------------------------------------------------------------
# Analysis runs
# --------------------------------------------------------------------------------------

@router.post("/runs", status_code=201)
def create_run(
    body: TimelinessRunRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """按观察截止日与申报群组创建一次时效分析运行（不可变快照）。

    迟到回执（截止日后接收、截止日前签发）会纳入新运行并显式标记；此前运行的覆盖边界
    保持不变。输出三类样本、可信度分层、等待/到账分布与分主体×币种×审核批次的右删失分析。
    """
    entity_id = None
    if body.entity_code:
        ent = _entity(db, body.entity_code)
        principal.require_entity(ent.id)
        entity_id = ent.id
    elif not principal.is_admin:
        raise HTTPException(403, "entity_code required for non-admin principals")

    group_filter = {
        "review_batch_nos": body.review_batch_nos,
        "currencies": body.currencies,
        "export_line_ids": body.export_line_ids,
    }
    run = compiler.build_run(
        db,
        entity_id=entity_id,
        actor=principal.name,
        cutoff=body.observation_cutoff,
        group_filter=group_filter,
        mature_horizon_days=body.mature_horizon_days,
    )
    db.commit()
    return {"run_id": run.id, "input_fingerprint": run.input_fingerprint, "result": run.result}


@router.get("/runs")
def list_runs(
    entity_code: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出时效分析运行（按创建倒序）。"""
    stmt = select(m.TimelinessRun).order_by(m.TimelinessRun.id.desc())
    if entity_code:
        ent = _entity(db, entity_code)
        principal.require_entity(ent.id)
        stmt = stmt.where(m.TimelinessRun.entity_id == ent.id)
    elif not principal.is_admin:
        scoped = [e.id for e in db.scalars(select(m.Entity)).all() if principal.can_act_on(e.id)]
        stmt = stmt.where(m.TimelinessRun.entity_id.in_(scoped))
    rows = db.scalars(stmt.limit(200)).all()
    return [
        jsonable(
            {
                "run_id": r.id,
                "entity_id": r.entity_id,
                "run_by": r.run_by,
                "run_at": r.run_at,
                "observation_cutoff": r.observation_cutoff,
                "group_filter": r.group_filter,
                "mature_horizon_days": r.mature_horizon_days,
                "status": r.status,
                "input_fingerprint": r.input_fingerprint,
                "sample_counts": r.result.get("sample_counts") if r.result else None,
                "tier_counts": r.result.get("tier_counts") if r.result else None,
            }
        )
        for r in rows
    ]


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """读取一次分析运行的完整结果（行级事实、分层、分布、KM 右删失风险集）。"""
    run = _load_run(db, run_id, principal)
    return jsonable(
        {
            "run_id": run.id,
            "entity_id": run.entity_id,
            "run_by": run.run_by,
            "run_at": run.run_at,
            "observation_cutoff": run.observation_cutoff,
            "group_filter": run.group_filter,
            "mature_horizon_days": run.mature_horizon_days,
            "input_fingerprint": run.input_fingerprint,
            "result": run.result,
        }
    )


@router.get("/runs/{run_id}/lines/{export_line_id}")
def line_drilldown(
    run_id: int,
    export_line_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """单条记录回查：回执序列、银行覆盖边界、主任务到账归属状态、候选关联依据、可信度分层与样本资格。"""
    run = _load_run(db, run_id, principal)
    export = db.get(m.ExportLine, export_line_id)
    if export is None:
        raise HTTPException(404, "export line not found")
    principal.require_entity(export.entity_id)
    return compiler.line_drilldown(db, run, export_line_id)


@router.get("/runs/{run_id}/survival")
def survival_points(
    run_id: int,
    metric: str | None = Query(default=None, pattern="^(first_receipt|full_receipt)$"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """读取右删失风险集推导点（风险集人数、事件数、删失数、生存比例与 Greenwood 区间）。"""
    run = _load_run(db, run_id, principal)
    stmt = (
        select(m.TimelinessSurvivalPoint)
        .where(m.TimelinessSurvivalPoint.run_id == run.id)
        .order_by(
            m.TimelinessSurvivalPoint.metric,
            m.TimelinessSurvivalPoint.entity_code,
            m.TimelinessSurvivalPoint.currency,
            m.TimelinessSurvivalPoint.review_batch_no,
            m.TimelinessSurvivalPoint.t_days,
        )
    )
    if metric:
        stmt = stmt.where(m.TimelinessSurvivalPoint.metric == metric)
    rows = db.scalars(stmt).all()
    return [
        jsonable(
            {
                "metric": r.metric,
                "entity_code": r.entity_code,
                "currency": r.currency,
                "review_batch_no": r.review_batch_no,
                "t_days": r.t_days,
                "n_risk": r.n_risk,
                "d_events": r.d_events,
                "c_censored": r.c_censored,
                "survival": r.survival,
                "ci_lower": r.ci_lower,
                "ci_upper": r.ci_upper,
                "cumulative_events": r.cumulative_events,
            }
        )
        for r in rows
    ]
