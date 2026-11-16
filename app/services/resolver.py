"""Line-level identity resolver: assembles evidence-graded candidate dossiers.

Evidence grades:
  A — machine-unique, single-source line identity (declaration line no, unique payment reference)
  B — constrained multi-attribute agreement (entity + currency + contract + export date + tax receipt)
  C — weak/ambiguous (single link, no cross-confirmation)
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..schemas import IntakeLine
from ..util import jsonable, min_grade
from .intake import parse_unique_reference


def _grade_for_links(*, customs_match: bool, tax_match: bool, invoice_match: bool, amount_match: bool) -> str:
    score = sum([customs_match, tax_match, invoice_match, amount_match])
    if customs_match and tax_match:
        return "A"
    if score >= 2:
        return "B"
    return "C"


def resolve_all(db: Session, entity_id: int) -> list[int]:
    """Build/refresh dossiers for one entity's export lines. Idempotent."""
    export_lines = db.scalars(
        select(m.ExportLine).where(m.ExportLine.entity_id == entity_id).order_by(m.ExportLine.id)
    ).all()
    dossier_ids: list[int] = []
    for export in export_lines:
        dossier = build_export_dossier(db, export)
        dossier_ids.append(dossier.id)
    bank_txns = db.scalars(
        select(m.BankTransaction).where(m.BankTransaction.entity_id == entity_id).order_by(m.BankTransaction.id)
    ).all()
    for txn in bank_txns:
        unique = _unique_target(db, txn)
        dossier_ids.extend(_build_bank_dossier(db, txn, unique))
    db.flush()
    return dossier_ids


def build_export_dossier(db: Session, export: m.ExportLine) -> m.Dossier:
    ref = f"EXP-{export.id}"
    dossier = db.scalar(select(m.Dossier).where(m.Dossier.case_ref == ref))
    if dossier is None:
        dossier = m.Dossier(entity_id=export.entity_id, case_ref=ref, export_line_id=export.id)
        db.add(dossier)
        db.flush()

    members: list[dict] = []
    customs_match = tax_match = invoice_match = False
    qty_mismatch = False

    if export.invoice_line_id:
        invoice_line = db.get(m.InvoiceLine, export.invoice_line_id)
        if invoice_line:
            members.append(
                {
                    "type": "invoice_line",
                    "id": invoice_line.id,
                    "grade": "A",
                    "detail": {
                        "line_no": invoice_line.line_no,
                        "amount": invoice_line.amount,
                        "quantity": invoice_line.quantity,
                    },
                }
            )
            invoice_match = True

    packing_rows = db.scalars(
        select(m.PackingLine).where(m.PackingLine.export_line_id == export.id).order_by(m.PackingLine.id)
    ).all()
    for pack in packing_rows:
        same_qty = pack.quantity == export.declared_quantity
        qty_mismatch = qty_mismatch or not same_qty
        members.append(
            {
                "type": "packing_line",
                "id": pack.id,
                "grade": "B" if same_qty else "C",
                "detail": {
                    "line_no": pack.line_no,
                    "quantity": pack.quantity,
                    "packages": pack.packages,
                    "net_weight": pack.net_weight,
                    "quantity_agrees_with_declaration": same_qty,
                },
            }
        )

    if export.customs_line_id:
        customs = db.get(m.CustomsLine, export.customs_line_id)
        if customs:
            members.append(
                {
                    "type": "customs_line",
                    "id": customs.id,
                    "grade": "A",
                    "detail": {
                        "line_no": customs.line_no,
                        "declaration_no": customs.declaration_no,
                        "revision_no": customs.revision_no,
                        "amount": customs.amount,
                    },
                }
            )
            customs_match = True

    tax_lines = db.scalars(
        select(m.TaxLine).where(m.TaxLine.export_line_id == export.id).order_by(m.TaxLine.issue_time)
    ).all()
    for tax in tax_lines:
        members.append(
            {
                "type": "tax_line",
                "id": tax.id,
                "grade": "B" if tax.amount == export.declared_amount else "C",
                "detail": {
                    "batch_no": tax.batch_no,
                    "review_status": tax.review_status,
                    "amount": tax.amount,
                    "order_conflict": tax.order_conflict,
                },
            }
        )
        tax_match = tax_match or tax.amount == export.declared_amount

    amount_match = any(
        mem["detail"].get("amount") is not None
        and Decimal(str(mem["detail"]["amount"])) == export.declared_amount
        for mem in members
    )
    grade = _grade_for_links(
        customs_match=customs_match,
        tax_match=tax_match,
        invoice_match=invoice_match,
        amount_match=amount_match,
    )
    dossier.evidence_grade = min_grade(grade, *[mem["grade"] for mem in members] or grade)

    existing = {(mem.member_type, mem.member_id): mem for mem in _members(db, dossier.id)}
    for mem in members:
        key = (mem["type"], mem["id"])
        if key in existing:
            obj = existing[key]
            obj.evidence_grade = mem["grade"]
            obj.detail = jsonable(mem["detail"])
        else:
            db.add(
                m.DossierMember(
                    dossier_id=dossier.id,
                    member_type=mem["type"],
                    member_id=mem["id"],
                    evidence_grade=mem["grade"],
                    detail=jsonable(mem["detail"]),
                )
            )
    dossier.summary = jsonable(
        {
            "export_line_id": export.id,
            "declaration_no": export.declaration_no,
            "invoice_no": export.invoice_no,
            "currency": export.currency,
            "declared_amount": export.declared_amount,
            "member_count": len(members),
            "packing_quantity_mismatch": qty_mismatch,
        }
    )
    db.flush()
    return dossier


def _members(db: Session, dossier_id: int) -> list[m.DossierMember]:
    return list(db.scalars(select(m.DossierMember).where(m.DossierMember.dossier_id == dossier_id)).all())


def _unique_target(db: Session, txn: m.BankTransaction) -> m.ExportLine | None:
    targets = unique_targets(db, txn)
    return targets[0] if len(targets) == 1 else None


def unique_targets(db: Session, txn: m.BankTransaction) -> list[m.ExportLine]:
    parsed = parse_unique_reference(txn.remittance_info)
    if not parsed:
        return []
    stmt = select(m.ExportLine).where(m.ExportLine.entity_id == txn.entity_id)
    if "declaration_no" in parsed:
        stmt = stmt.where(
            m.ExportLine.declaration_no == parsed["declaration_no"],
        )
        rows = list(db.scalars(stmt).all())
        if parsed.get("line_no") is not None:
            rows = [r for r in rows if _customs_line_no(db, r) == parsed["line_no"]]
        return rows
    if "sku" in parsed:
        rows = list(db.scalars(stmt.where(m.ExportLine.sku == parsed["sku"])).all())
        return rows
    if "invoice_no" in parsed:
        rows = list(
            db.scalars(stmt.where(m.ExportLine.invoice_no == parsed["invoice_no"])).all()
        )
        return [r for r in rows if _invoice_line_no(db, r) == parsed["invoice_line_no"]]
    return []


def _customs_line_no(db: Session, export: m.ExportLine) -> int | None:
    if not export.customs_line_id:
        return None
    c = db.get(m.CustomsLine, export.customs_line_id)
    return c.line_no if c else None


def _invoice_line_no(db: Session, export: m.ExportLine) -> int | None:
    if not export.invoice_line_id:
        return None
    i = db.get(m.InvoiceLine, export.invoice_line_id)
    return i.line_no if i else None


def _build_bank_dossier(db: Session, txn: m.BankTransaction, unique: m.ExportLine | None) -> list[int]:
    if unique is None:
        return []
    ref = f"BNK-{txn.id}-EXP-{unique.id}"
    dossier = db.scalar(select(m.Dossier).where(m.Dossier.case_ref == ref))
    if dossier is None:
        dossier = m.Dossier(
            entity_id=txn.entity_id, case_ref=ref, bank_transaction_id=txn.id, export_line_id=unique.id
        )
        db.add(dossier)
        db.flush()
    grade = "A" if txn.remittance_info and unique.declared_amount == txn.amount else "B"
    dossier.evidence_grade = grade
    dossier.summary = jsonable(
        {
            "bank_transaction_id": txn.id,
            "export_line_id": unique.id,
            "currency": txn.currency,
            "amount": txn.amount,
            "remittance_info": txn.remittance_info,
        }
    )
    existing = {(x.member_type, x.member_id) for x in _members(db, dossier.id)}
    for kind, member_id, detail in (
        ("bank_transaction", txn.id, {"txn_ref": txn.txn_ref, "amount": txn.amount}),
        ("export_line", unique.id, {"declaration_no": unique.declaration_no, "amount": unique.declared_amount}),
    ):
        if (kind, member_id) not in existing:
            db.add(
                m.DossierMember(
                    dossier_id=dossier.id,
                    member_type=kind,
                    member_id=member_id,
                    evidence_grade=grade,
                    detail=jsonable(detail),
                )
            )
    db.flush()
    return [dossier.id]


def constrained_candidates(db: Session, txn: m.BankTransaction) -> list[dict]:
    """List constrained allocation candidates (never auto-allocated) for a combined remittance.

    Constraints: same entity, same currency, contract present, export date on/before value date,
    and a tax review line exists (accepted review). Candidates are ordered by evidence strength.
    """
    stmt = select(m.ExportLine).where(
        m.ExportLine.entity_id == txn.entity_id,
        m.ExportLine.currency == txn.currency,
        m.ExportLine.contract_id.is_not(None),
    )
    rows = list(db.scalars(stmt.order_by(m.ExportLine.id)).all())
    bank_bal = db.scalar(select(m.Balance).where(m.Balance.side == "bank_transaction", m.Balance.ref_id == txn.id))
    results: list[dict] = []
    for export in rows:
        reasons: list[str] = []
        grade = "C"
        if export.export_date is not None and export.export_date > txn.value_date:
            continue
        export_bal = db.scalar(
            select(m.Balance).where(m.Balance.side == "export_line", m.Balance.ref_id == export.id)
        )
        if export_bal is None or export_bal.available <= 0 or bank_bal is None or bank_bal.available <= 0:
            continue
        tax = db.scalar(
            select(m.TaxLine)
            .where(
                m.TaxLine.export_line_id == export.id,
                m.TaxLine.review_status == "approved",
                m.TaxLine.order_conflict.is_(False),
            )
            .order_by(m.TaxLine.issue_time)
            .limit(1)
        )
        contract = db.get(m.Contract, export.contract_id) if export.contract_id else None
        matched = 0
        if contract is not None:
            matched += 1
            reasons.append(f"contract {contract.contract_no} active for entity/currency")
        if export.export_date is not None:
            matched += 1
            reasons.append(
                f"export date {export.export_date.date()} precedes value date {txn.value_date.date()}"
            )
        if tax is not None:
            matched += 1
            reasons.append(f"tax receipt batch {tax.batch_no} accepted")
        if export.declared_amount == txn.amount:
            matched += 1
            reasons.append("amount equals whole remittance (suggestive only, not proof)")
        if matched >= 3:
            grade = "B"
        else:
            reasons.append("insufficient constraints for strong linkage")
        results.append(
            {
                "export_line_id": export.id,
                "evidence_grade": grade,
                "constraints_matched": matched,
                "proposed_amount": str(min(export_bal.available, bank_bal.available)),
                "declared_amount": str(export.declared_amount),
                "open_export_amount": str(export_bal.available),
                "currency": export.currency,
                "reasons": reasons,
                "requires_analyst_confirmation": True,
            }
        )
    results.sort(key=lambda r: (r["constraints_matched"], r["export_line_id"]), reverse=True)
    return results


def create_reverse_candidate(db: Session, entity: m.Entity, doc: m.SourceDocument, req) -> m.ReverseCandidate:
    """Return-shipment evidence after declaration -> reverse quantity candidate, never auto-applied.

    Compares effective time of the return document, transport timing (shipped/returned) and the
    original line evidence. Only when the attribution is unique does unique_attribution become true,
    and even then it stays a candidate.
    """
    lines = req.lines or [IntakeLine()]
    line = lines[0]
    if line.quantity is None:
        raise HTTPException(422, "return document requires quantity")
    export = None
    if line.declaration_no:
        rows = list(
            db.scalars(
                select(m.ExportLine).where(
                    m.ExportLine.entity_id == entity.id,
                    m.ExportLine.declaration_no == line.declaration_no,
                )
            ).all()
        )
        export = rows[0] if len(rows) == 1 else None
    elif line.invoice_no:
        rows = list(
            db.scalars(
                select(m.ExportLine).where(
                    m.ExportLine.entity_id == entity.id,
                    m.ExportLine.invoice_no == line.invoice_no,
                )
            ).all()
        )
        export = rows[0] if len(rows) == 1 else None

    reasons: list[str] = []
    effective = doc.event_time or line.transport_returned_date
    unique = export is not None
    if export is None:
        reasons.append("no unique export line match for return document")
    if effective is None:
        reasons.append("return document has no provable effective time")
        unique = False
    if line.transport_shipped_date and line.transport_returned_date:
        if line.transport_returned_date < line.transport_shipped_date:
            reasons.append("transport timing is inconsistent (returned before shipped)")
            unique = False
    if export is not None and effective is not None and export.export_date is not None:
        reasons.append(
            f"return effective time {effective.isoformat()} vs export date {export.export_date.isoformat()}"
        )
        if effective < export.export_date:
            reasons.append("return effective time precedes export date — eligibility contradiction retained")
        if doc.received_time and effective and doc.received_time < effective:
            reasons.append("received before effective time — receipt timing alone cannot retire eligibility")
    reasons.append("reverse quantity kept as candidate; not applied to declared quantity or settled amount")

    ledger = None
    if export is not None:
        ledger = db.scalar(select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export.id))
    if ledger is not None and ledger.submitted_at is None:
        unique = False
        reasons.append("original line was not yet submitted when return evidence arrived")

    grade = "B" if unique else "C"
    candidate = m.ReverseCandidate(
        entity_id=entity.id,
        export_line_id=export.id if export else None,
        document_id=doc.id,
        effective_time=effective,
        quantity=line.quantity,
        amount=line.amount,
        currency=line.currency,
        unique_attribution=unique,
        evidence_grade=grade,
        reasons=jsonable(reasons),
    )
    db.add(candidate)
    db.flush()
    return candidate
