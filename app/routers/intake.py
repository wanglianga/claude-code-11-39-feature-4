"""Intake ledger endpoints (append-only source documents)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import Principal, get_principal, require_writer
from ..db import get_db
from ..schemas import IntakeRequest
from ..services.intake import ingest_document
from ..util import jsonable

router = APIRouter(prefix="/api/v1/intake", tags=["intake"])


@router.post("/documents", status_code=201)
def post_document(
    body: IntakeRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_writer),
):
    """写入一份来源文档及其行到 append-only 受理账。支持 invoice / packing_list / customs_archive / customs_correction / tax_receipt / bank_statement / entity_directory / contract / return_document。海关更正行通过 revises_line_no 指向当前有效行并自动建立修订链。"""
    if body.entity_code:
        ent = db.scalar(select(m.Entity).where(m.Entity.code == body.entity_code))
        if ent is None:
            raise HTTPException(422, f"entity_code not found: {body.entity_code}")
        principal.require_entity(ent.id)
    elif principal.role != "admin":
        raise HTTPException(403, "entity_code is required for non-admin principals")

    result = ingest_document(db, body, principal.name)
    db.commit()
    return result


@router.get("/documents")
def list_documents(
    doc_type: str | None = None,
    entity_code: str | None = None,
    external_no: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """按类型、外部编号或主体查询受理账文档（按接收时间倒序）。"""
    stmt = select(m.SourceDocument).order_by(m.SourceDocument.received_time.desc(), m.SourceDocument.id.desc())
    if doc_type:
        stmt = stmt.where(m.SourceDocument.doc_type == doc_type)
    if external_no:
        stmt = stmt.where(m.SourceDocument.external_no == external_no)
    if entity_code:
        ent = _entity_or_404(db, entity_code)
        principal.require_entity(ent.id)
        stmt = stmt.where(m.SourceDocument.entity_id == ent.id)
    rows = db.scalars(stmt.limit(min(limit, 500))).all()
    rows = [r for r in rows if r.entity_id is None or principal.can_act_on(r.entity_id)]
    return [_doc_view(r) for r in rows]


@router.get("/documents/{document_id}")
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """返回文档主记录、全部行（发票/报关/装箱/税务/银行）以及报关行所属修订链。"""
    doc = db.get(m.SourceDocument, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.entity_id is not None:
        principal.require_entity(doc.entity_id)
    out = _doc_view(doc)
    out["lines"] = _document_lines(db, doc)
    out["correction_chain"] = _correction_chain(db, doc)
    return out


def _entity_or_404(db: Session, code: str) -> m.Entity:
    ent = db.scalar(select(m.Entity).where(m.Entity.code == code))
    if ent is None:
        raise HTTPException(404, f"entity not found: {code}")
    return ent


def _doc_view(doc: m.SourceDocument) -> dict:
    return jsonable(
        {
            "id": doc.id,
            "doc_type": doc.doc_type,
            "entity_id": doc.entity_id,
            "external_no": doc.external_no,
            "original_location": doc.original_location,
            "event_time": doc.event_time,
            "received_time": doc.received_time,
            "currency": doc.currency,
            "amount": doc.amount,
            "quantity": doc.quantity,
            "sha256": doc.sha256,
            "source_version": doc.source_version,
            "supersedes_id": doc.supersedes_id,
            "meta": doc.meta,
            "ingested_by": doc.ingested_by,
        }
    )


def _document_lines(db: Session, doc: m.SourceDocument) -> dict:
    invoice = db.scalars(select(m.InvoiceLine).where(m.InvoiceLine.document_id == doc.id).order_by(m.InvoiceLine.line_no)).all()
    customs = db.scalars(select(m.CustomsLine).where(m.CustomsLine.document_id == doc.id).order_by(m.CustomsLine.line_no)).all()
    tax = db.scalars(select(m.TaxLine).where(m.TaxLine.document_id == doc.id).order_by(m.TaxLine.line_no)).all()
    bank = db.scalars(select(m.BankTransaction).where(m.BankTransaction.document_id == doc.id)).all()
    packing = db.scalars(select(m.PackingLine).where(m.PackingLine.document_id == doc.id).order_by(m.PackingLine.line_no)).all()
    return jsonable(
        {
            "invoice_lines": [
                {
                    "id": r.id,
                    "line_no": r.line_no,
                    "sku": r.sku,
                    "description": r.description,
                    "quantity": r.quantity,
                    "amount": r.amount,
                    "currency": r.currency,
                    "contract_id": r.contract_id,
                }
                for r in invoice
            ],
            "customs_lines": [
                {
                    "id": r.id,
                    "line_no": r.line_no,
                    "declaration_no": r.declaration_no,
                    "hs_code": r.hs_code,
                    "description": r.description,
                    "quantity": r.quantity,
                    "amount": r.amount,
                    "currency": r.currency,
                    "revision_no": r.revision_no,
                    "revises_id": r.revises_id,
                    "is_current": r.is_current,
                    "changed_fields": r.changed_fields,
                }
                for r in customs
            ],
            "tax_lines": [
                {
                    "id": r.id,
                    "line_no": r.line_no,
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
                for r in tax
            ],
            "bank_transactions": [
                {
                    "id": r.id,
                    "txn_ref": r.txn_ref,
                    "amount": r.amount,
                    "currency": r.currency,
                    "value_date": r.value_date,
                    "remittance_info": r.remittance_info,
                    "has_line_unique_reference": r.has_line_unique_reference,
                }
                for r in bank
            ],
            "packing_lines": [
                {
                    "id": r.id,
                    "line_no": r.line_no,
                    "sku": r.sku,
                    "quantity": r.quantity,
                    "packages": r.packages,
                    "net_weight": r.net_weight,
                    "export_line_id": r.export_line_id,
                }
                for r in packing
            ],
        }
    )


def _correction_chain(db: Session, doc: m.SourceDocument) -> list[dict]:
    rows = db.scalars(
        select(m.CustomsLine).where(m.CustomsLine.document_id == doc.id).order_by(m.CustomsLine.line_no)
    ).all()
    out = []
    for line in rows:
        chain = []
        cur = line
        seen = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            chain.append(
                {
                    "customs_line_id": cur.id,
                    "source_document_id": cur.document_id,
                    "revision_no": cur.revision_no,
                    "amount": str(cur.amount),
                    "quantity": str(cur.quantity),
                    "is_current": cur.is_current,
                }
            )
            cur = db.get(m.CustomsLine, cur.revises_id) if cur.revises_id else None
        out.append({"customs_line_id": line.id, "chain": list(reversed(chain))})
    return out
