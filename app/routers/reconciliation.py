"""Row-level dossiers, reconciliation pass, candidates, confirmations, facts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import Principal, get_principal, require_writer
from ..db import get_db
from ..schemas import CompileRequest, ConfirmRequest, DeclarationEventRequest, ReassignRequest, VoidRequest
from ..services import balance, facts, ledgers, resolver
from ..util import jsonable

router = APIRouter(prefix="/api/v1", tags=["reconciliation"])


def _entity(db: Session, code: str) -> m.Entity:
    ent = db.scalar(select(m.Entity).where(m.Entity.code == code))
    if ent is None:
        raise HTTPException(404, f"entity not found: {code}")
    return ent


def _scoped_entities(db: Session, principal: Principal) -> list[m.Entity]:
    rows = db.scalars(select(m.Entity).order_by(m.Entity.code)).all()
    return [r for r in rows if principal.can_act_on(r.id)]


@router.post("/entities/{entity_code}/resolve", status_code=201)
def run_resolver(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """运行行级身份解析器：为每个出口行与唯一关联的银行流水生成/刷新带证据等级（A/B/C）的候选案卷。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    ids = resolver.resolve_all(db, ent.id)
    db.commit()
    return {"entity_code": entity_code, "dossier_ids": ids}


@router.get("/entities/{entity_code}/dossiers")
def list_dossiers(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出某主体的候选案卷及其证据等级与摘要。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    rows = db.scalars(select(m.Dossier).where(m.Dossier.entity_id == ent.id).order_by(m.Dossier.id)).all()
    return [
        jsonable(
            {
                "id": d.id,
                "case_ref": d.case_ref,
                "export_line_id": d.export_line_id,
                "bank_transaction_id": d.bank_transaction_id,
                "evidence_grade": d.evidence_grade,
                "status": d.status,
                "summary": d.summary,
            }
        )
        for d in rows
    ]


@router.get("/dossiers/{dossier_id}")
def dossier_detail(
    dossier_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """展开单个候选案卷：包含发票行、报关行、税务回执行、装箱行、银行流水等成员及其证据等级。"""
    dossier = db.get(m.Dossier, dossier_id)
    if dossier is None:
        raise HTTPException(404, "dossier not found")
    principal.require_entity(dossier.entity_id)
    members = db.scalars(
        select(m.DossierMember).where(m.DossierMember.dossier_id == dossier.id).order_by(m.DossierMember.id)
    ).all()
    details = []
    for mem in members:
        details.append(
            {
                "member_type": mem.member_type,
                "member_id": mem.member_id,
                "evidence_grade": mem.evidence_grade,
                "detail": mem.detail,
            }
        )
    return jsonable(
        {
            "id": dossier.id,
            "case_ref": dossier.case_ref,
            "entity_id": dossier.entity_id,
            "export_line_id": dossier.export_line_id,
            "bank_transaction_id": dossier.bank_transaction_id,
            "evidence_grade": dossier.evidence_grade,
            "summary": dossier.summary,
            "members": details,
        }
    )


@router.post("/entities/{entity_code}/reconcile-pass")
def reconcile_pass(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """Order of operations: confirm unique links first, then generate candidates for combined remittances."""
    """Order of operations: confirm unique links first, then generate candidates for combined remittances."""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    result = balance.run_reconciliation_pass(db, entity_id=ent.id, actor=principal.name)
    db.commit()
    return jsonable(result)


@router.get("/entities/{entity_code}/candidates")
def list_candidates(
    entity_code: str,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出候选关联（默认全部状态），包含策略、证据等级、建议金额与生成理由。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    stmt = (
        select(m.ReconciliationCandidate, m.BankTransaction)
        .join(m.BankTransaction, m.BankTransaction.id == m.ReconciliationCandidate.bank_transaction_id)
        .where(m.ReconciliationCandidate.entity_id == ent.id)
        .order_by(m.ReconciliationCandidate.id)
    )
    if status:
        stmt = stmt.where(m.ReconciliationCandidate.status == status)
    rows = db.execute(stmt).all()
    return [
        jsonable(
            {
                "id": c.id,
                "bank_transaction_id": c.bank_transaction_id,
                "txn_ref": t.txn_ref,
                "bank_amount": t.amount,
                "export_line_id": c.export_line_id,
                "strategy": c.strategy,
                "evidence_grade": c.evidence_grade,
                "proposed_amount": c.proposed_amount,
                "currency": c.currency,
                "status": c.status,
                "reasons": c.reasons,
                "created_by": c.created_by,
            }
        )
        for c, t in rows
    ]


@router.post("/candidates/{candidate_id}/confirm", status_code=201)
def confirm_candidate(
    candidate_id: int,
    body: ConfirmRequest | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """确认候选（可部分确认）。在同一事务内按字典序锁定银行流水与出口行两侧余额行，校验累计确认额不超过任一侧实际可用金额后原子占用；越界返回 409 并记录 over_allocation_attempt 差异。"""
    candidate = db.get(m.ReconciliationCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    principal.require_entity(candidate.entity_id)
    amount = body.amount if body else None
    note = body.note if body else None
    try:
        line = balance.confirm_candidate(
            db, actor=principal.name, candidate_id=candidate_id, amount=amount, note=note
        )
    except balance.OverAllocationError as exc:
        db.commit()  # persist the conflict case recording the rejected attempt
        raise HTTPException(409, exc.detail)
    db.commit()
    return jsonable(
        {
            "reconciliation_line_id": line.id,
            "amount": line.amount,
            "currency": line.currency,
            "bank_transaction_id": line.bank_transaction_id,
            "export_line_id": line.export_line_id,
            "strategy": line.strategy,
            "evidence_grade": line.evidence_grade,
        }
    )


@router.post("/reconciliation-lines/{line_id}/void")
def void_line(
    line_id: int,
    body: VoidRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """撤销对账行并按 balance_holds 审计记录释放两侧占用金额；出口行余额上限回落到申报金额。"""
    line = db.get(m.ReconciliationLine, line_id)
    if line is None:
        raise HTTPException(404, "reconciliation line not found")
    principal.require_entity(line.entity_id)
    out = balance.void_line(db, actor=principal.name, line_id=line_id, reason=body.reason)
    db.commit()
    return {"reconciliation_line_id": out.id, "status": out.status, "void_reason": out.void_reason}


@router.post("/reconciliation-lines/{line_id}/reassign")
def reassign_line(
    line_id: int,
    body: ReassignRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """把已确认/复审中的占用金额原子转移到另一条出口行（同主体），原行标记 superseded，全过程写入余额流水与审计日志。"""
    line = db.get(m.ReconciliationLine, line_id)
    if line is None:
        raise HTTPException(404, "reconciliation line not found")
    principal.require_entity(line.entity_id)
    new = balance.reassign_line(
        db,
        actor=principal.name,
        line_id=line_id,
        target_export_line_id=body.target_export_line_id,
        reason=body.reason,
    )
    db.commit()
    return {
        "reconciliation_line_id": new.id,
        "export_line_id": new.export_line_id,
        "amount": str(new.amount),
        "superseded_line_id": line_id,
    }


@router.post("/reconciliation-lines/{line_id}/review")
def flag_review(
    line_id: int,
    body: VoidRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """Move an active confirmed line into review (occupancy frozen, excluded from facts)."""
    """Move an active confirmed line into review (occupancy frozen, excluded from facts)."""
    line = db.get(m.ReconciliationLine, line_id)
    if line is None:
        raise HTTPException(404, "reconciliation line not found")
    principal.require_entity(line.entity_id)
    if line.status != "active":
        raise HTTPException(409, f"line is {line.status}, only active lines can enter review")
    count = balance.flag_lines_for_review(
        db, line_ids=[line.id], reason=body.reason, kind="manual_review"
    )
    db.commit()
    return {"frozen_lines": count, "reason": body.reason}


@router.get("/reconciliation-lines/{line_id}")
def line_detail(
    line_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """对账行全展开：来源文件、申报/到账两套状态账与历史、两侧余额占用与流水、分摊情景、报关修订链、判定规则。"""
    line = db.get(m.ReconciliationLine, line_id)
    if line is None:
        raise HTTPException(404, "reconciliation line not found")
    principal.require_entity(line.entity_id)
    return facts.fact_detail(db, line)


@router.get("/entities/{entity_code}/balances")
def balances(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出某主体银行流水侧与原出口行侧的 total/committed/available 及版本号。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    bank = db.execute(
        select(m.Balance, m.BankTransaction)
        .join(m.BankTransaction, m.BankTransaction.id == m.Balance.ref_id)
        .where(m.Balance.side == "bank_transaction", m.BankTransaction.entity_id == ent.id)
        .order_by(m.Balance.id)
    ).all()
    export = db.execute(
        select(m.Balance, m.ExportLine)
        .join(m.ExportLine, m.ExportLine.id == m.Balance.ref_id)
        .where(m.Balance.side == "export_line", m.ExportLine.entity_id == ent.id)
        .order_by(m.Balance.id)
    ).all()
    return jsonable(
        {
            "bank_transactions": [
                {
                    "bank_transaction_id": t.id,
                    "txn_ref": t.txn_ref,
                    "total": b.total,
                    "committed": b.committed,
                    "available": b.available,
                    "version": b.version,
                }
                for b, t in bank
            ],
            "export_lines": [
                {
                    "export_line_id": e.id,
                    "declaration_no": e.declaration_no,
                    "total": b.total,
                    "committed": b.committed,
                    "available": b.available,
                    "version": b.version,
                }
                for b, e in export
            ],
        }
    )


@router.post("/facts/compile")
def compile_facts(
    body: CompileRequest | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """编译事实快照：只发布 active 对账行为已证实事实，并输出未分摊余额（银行侧与出口行侧）和全部差异原因。候选、冻结行、退运候选不会计入已证实到账量。"""
    entity_id = None
    if body and body.entity_code:
        ent = _entity(db, body.entity_code)
        principal.require_entity(ent.id)
        entity_id = ent.id
    elif not principal.is_admin:
        raise HTTPException(403, "entity_code required for non-admin principals")
    run = facts.compile_facts(db, entity_id=entity_id, actor=principal.name)
    db.commit()
    return {"compile_run_id": run.id, "result": run.result, "counts": {
        "facts": run.facts_count,
        "unallocated": run.unallocated_count,
        "differences": run.differences_count,
    }}


@router.get("/facts/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
    """按 ID 读取历史编译快照。"""
    run = db.get(m.CompileRun, run_id)
    if run is None:
        raise HTTPException(404, "compile run not found")
    if run.entity_id is not None:
        principal.require_entity(run.entity_id)
    return jsonable(
        {
            "id": run.id,
            "entity_id": run.entity_id,
            "run_by": run.run_by,
            "run_at": run.run_at,
            "counts": {
                "facts": run.facts_count,
                "unallocated": run.unallocated_count,
                "differences": run.differences_count,
            },
            "result": run.result,
        }
    )


@router.get("/facts/latest")
def latest_facts(
    entity_code: str | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """读取最新一次编译快照，可按主体过滤。"""
    stmt = select(m.CompileRun).order_by(m.CompileRun.id.desc())
    if entity_code:
        ent = _entity(db, entity_code)
        principal.require_entity(ent.id)
        stmt = stmt.where(m.CompileRun.entity_id == ent.id)
    else:
        scoped = [e.id for e in _scoped_entities(db, principal)]
        stmt = stmt.where(m.CompileRun.entity_id.in_(scoped))
    run = db.scalar(stmt.limit(1))
    if run is None:
        raise HTTPException(404, "no compile run found")
    return get_run(run.id, db=db, principal=principal)


@router.get("/entities/{entity_code}/status-ledgers")
def status_ledgers(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """分别返回申报状态账（draft/submitted/verified/rejected/correction_pending）、到账状态账（unmatched/pending_allocation/partial/matched/conflict）与出口行到账概况。两套账互不共享已结清字段。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    decl = db.execute(
        select(m.DeclarationLedger, m.ExportLine)
        .join(m.ExportLine, m.ExportLine.id == m.DeclarationLedger.export_line_id)
        .where(m.ExportLine.entity_id == ent.id)
        .order_by(m.DeclarationLedger.id)
    ).all()
    receipts = db.execute(
        select(m.ReceiptLedger, m.BankTransaction)
        .join(m.BankTransaction, m.BankTransaction.id == m.ReceiptLedger.bank_transaction_id)
        .where(m.BankTransaction.entity_id == ent.id)
        .order_by(m.ReceiptLedger.id)
    ).all()

    def export_receipt_state(export_id: int) -> str:
        bal = db.scalar(select(m.Balance).where(m.Balance.side == "export_line", m.Balance.ref_id == export_id))
        if bal is None or bal.committed == 0:
            return "not_received"
        frozen = db.scalar(
            select(m.ReconciliationLine.id).where(
                m.ReconciliationLine.export_line_id == export_id,
                m.ReconciliationLine.status == "needs_review",
            ).limit(1)
        )
        if frozen:
            return "conflict"
        return "partial" if bal.available > 0 else "received"

    export_lines = db.scalars(
        select(m.ExportLine).where(m.ExportLine.entity_id == ent.id).order_by(m.ExportLine.id)
    ).all()
    return jsonable(
        {
            "declaration_mode": "draft/submitted/verified/rejected/correction_pending",
            "receipt_mode": "unmatched/pending_allocation/partial/matched/conflict",
            "declaration_ledgers": [
                {
                    "export_line_id": d.export_line_id,
                    "declaration_no": e.declaration_no,
                    "state": d.state,
                    "correction_open": d.correction_open,
                    "submitted_at": d.submitted_at,
                    "reviewed_at": d.reviewed_at,
                    "version": d.version,
                }
                for d, e in decl
            ],
            "receipt_ledgers": [
                {
                    "bank_transaction_id": r.bank_transaction_id,
                    "txn_ref": t.txn_ref,
                    "state": r.state,
                    "pending_allocation": r.pending_allocation,
                    "conflict_open": r.conflict_open,
                    "version": r.version,
                }
                for r, t in receipts
            ],
            "export_line_receipts": [
                {
                    "export_line_id": e.id,
                    "declaration_no": e.declaration_no,
                    "declared_amount": str(e.declared_amount),
                    "receipt_state": export_receipt_state(e.id),
                }
                for e in export_lines
            ],
            "note": "declaration and receipt ledgers are separate ledgers and share no settlement columns",
        }
    )


@router.post("/export-lines/{export_line_id}/declaration-events", status_code=201)
def post_declaration_event(
    export_line_id: int,
    event: DeclarationEventRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """记录申报状态事件（submitted / verified / rejected / draft_recorded）。非法状态转移返回 409；事件写入 declaration_events 供后续回查。"""
    export = db.get(m.ExportLine, export_line_id)
    if export is None:
        raise HTTPException(404, "export line not found")
    principal.require_entity(export.entity_id)
    ledger = ledgers.apply_declaration_event(
        db,
        export,
        event.event,
        actor=principal.name,
        at=event.at,
        reference=event.reference,
        note=event.note,
        source_document_id=event.source_document_id,
    )
    db.commit()
    return {
        "export_line_id": export.id,
        "state": ledger.state,
        "correction_open": ledger.correction_open,
        "version": ledger.version,
    }


@router.get("/export-lines/{export_line_id}")
def export_line_detail(
    export_line_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """出口行详情：申报状态与历史、两侧余额、对账行、报关修订链。"""
    export = db.get(m.ExportLine, export_line_id)
    if export is None:
        raise HTTPException(404, "export line not found")
    principal.require_entity(export.entity_id)

    ledger = db.scalar(select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export.id))
    balance = db.scalar(
        select(m.Balance).where(m.Balance.side == "export_line", m.Balance.ref_id == export.id)
    )
    lines = db.scalars(
        select(m.ReconciliationLine)
        .where(m.ReconciliationLine.export_line_id == export.id)
        .order_by(m.ReconciliationLine.id)
    ).all()
    customs_chains = []
    if export.customs_line_id:
        cur = db.get(m.CustomsLine, export.customs_line_id)
        while cur is not None:
            customs_chains.append(
                {
                    "customs_line_id": cur.id,
                    "revision_no": cur.revision_no,
                    "amount": str(cur.amount),
                    "quantity": str(cur.quantity),
                    "is_current": cur.is_current,
                    "changed_fields": cur.changed_fields,
                }
            )
            cur = db.get(m.CustomsLine, cur.revises_id) if cur.revises_id else None
    return jsonable(
        {
            "id": export.id,
            "entity_id": export.entity_id,
            "declaration_no": export.declaration_no,
            "invoice_no": export.invoice_no,
            "sku": export.sku,
            "currency": export.currency,
            "declared_amount": export.declared_amount,
            "declared_quantity": export.declared_quantity,
            "evidence_grade": export.evidence_grade,
            "declaration_ledger": None
            if ledger is None
            else {
                "state": ledger.state,
                "correction_open": ledger.correction_open,
                "submitted_at": ledger.submitted_at,
                "reviewed_at": ledger.reviewed_at,
                "review_ref": ledger.review_ref,
                "review_note": ledger.review_note,
                "version": ledger.version,
                "history": ledgers.declaration_history(db, export.id),
            },
            "balance": None
            if balance is None
            else {
                "total": str(balance.total),
                "committed": str(balance.committed),
                "available": str(balance.available),
            },
            "reconciliation_lines": [
                {"id": l.id, "amount": str(l.amount), "status": l.status, "strategy": l.strategy}
                for l in lines
            ],
            "revision_chain": list(reversed(customs_chains)),
        }
    )


@router.get("/entities/{entity_code}/reverse-candidates")
def reverse_candidates(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出退运凭证生成的反向数量候选（含生效时间、运输时序分析与保留理由）。候选不改变申报数量、金额或已证实到账量。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    rows = db.scalars(
        select(m.ReverseCandidate).where(m.ReverseCandidate.entity_id == ent.id).order_by(m.ReverseCandidate.id)
    ).all()
    return [
        jsonable(
            {
                "id": r.id,
                "export_line_id": r.export_line_id,
                "document_id": r.document_id,
                "effective_time": r.effective_time,
                "quantity": r.quantity,
                "amount": r.amount,
                "currency": r.currency,
                "unique_attribution": r.unique_attribution,
                "evidence_grade": r.evidence_grade,
                "status": r.status,
                "reasons": r.reasons,
            }
        )
        for r in rows
    ]


@router.get("/entities/{entity_code}/packing-lines")
def packing_lines(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出装箱单行及其绑定的出口行（装箱证据只佐证数量）。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    rows = db.scalars(
        select(m.PackingLine).where(m.PackingLine.entity_id == ent.id).order_by(m.PackingLine.document_id,
                                                                               m.PackingLine.line_no)
    ).all()
    return [
        jsonable(
            {
                "id": r.id,
                "document_id": r.document_id,
                "line_no": r.line_no,
                "sku": r.sku,
                "quantity": r.quantity,
                "packages": r.packages,
                "net_weight": r.net_weight,
                "export_line_id": r.export_line_id,
            }
        )
        for r in rows
    ]


@router.get("/entities/{entity_code}/difference-cases")
def difference_cases(
    entity_code: str,
    state: str = "open",
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出差异原因（默认 open）：超额尝试、回执顺序冲突、更正后复核、占用超更正金额、装箱数量不符等。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    rows = db.scalars(
        select(m.DifferenceCase)
        .where(m.DifferenceCase.entity_id == ent.id, m.DifferenceCase.state == state)
        .order_by(m.DifferenceCase.id)
    ).all()
    return [
        jsonable(
            {
                "id": c.id,
                "kind": c.kind,
                "state": c.state,
                "ref_type": c.ref_type,
                "ref_id": c.ref_id,
                "detail": c.detail,
            }
        )
        for c in rows
    ]


@router.get("/entities/{entity_code}/tax-receipts")
def tax_receipts(
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """列出税务回执行：批次、签发时间、序号、已应用序号与顺序冲突标记。"""
    ent = _entity(db, entity_code)
    principal.require_entity(ent.id)
    rows = db.scalars(
        select(m.TaxLine).where(m.TaxLine.entity_id == ent.id).order_by(m.TaxLine.issue_time, m.TaxLine.id)
    ).all()
    return [
        jsonable(
            {
                "id": r.id,
                "batch_no": r.batch_no,
                "declaration_no": r.declaration_no,
                "review_status": r.review_status,
                "amount": r.amount,
                "currency": r.currency,
                "issue_time": r.issue_time,
                "sequence_no": r.sequence_no,
                "applied_sequence": r.applied_sequence,
                "order_conflict": r.order_conflict,
                "export_line_id": r.export_line_id,
            }
        )
        for r in rows
    ]
