"""Fact compiler: publishes only confirmed reconciliation facts, unallocated balances,
and difference reasons. Candidates and frozen lines are reported as pending, never as facts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..util import jsonable
from . import ledgers

ZERO = Decimal("0.00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compile_facts(db: Session, *, entity_id: int | None, actor: str) -> m.CompileRun:
    entities = (
        [db.scalar(select(m.Entity).where(m.Entity.id == entity_id))]
        if entity_id
        else list(db.scalars(select(m.Entity).order_by(m.Entity.id)).all())
    )
    entities = [e for e in entities if e is not None]

    facts: list[dict] = []
    unallocated: list[dict] = []
    differences: list[dict] = []

    for ent in entities:
        active_lines = db.scalars(
            select(m.ReconciliationLine)
            .where(
                m.ReconciliationLine.entity_id == ent.id,
                m.ReconciliationLine.status == "active",
            )
            .order_by(m.ReconciliationLine.id)
        ).all()
        for line in active_lines:
            txn = db.get(m.BankTransaction, line.bank_transaction_id)
            export = db.get(m.ExportLine, line.export_line_id)
            facts.append(_fact_payload(db, line, txn, export))

        # unallocated bank balances
        bank_rows = db.execute(
            select(m.Balance, m.BankTransaction)
            .join(m.BankTransaction, m.BankTransaction.id == m.Balance.ref_id)
            .where(m.Balance.side == "bank_transaction", m.BankTransaction.entity_id == ent.id)
            .order_by(m.Balance.id)
        ).all()
        for bal, txn in bank_rows:
            if bal.available > ZERO:
                receipt = db.scalar(
                    select(m.ReceiptLedger).where(m.ReceiptLedger.bank_transaction_id == txn.id)
                )
                unallocated.append(
                    {
                        "kind": "unallocated_bank_amount",
                        "entity_code": ent.code,
                        "bank_transaction_id": txn.id,
                        "txn_ref": txn.txn_ref,
                        "currency": txn.currency,
                        "transaction_amount": str(bal.total),
                        "unallocated_amount": str(bal.available),
                        "receipt_state": receipt.state if receipt else None,
                        "reason": _unallocated_reason(db, txn, bal, receipt),
                        "candidates": _open_candidate_briefs(db, txn.id),
                    }
                )

        # unallocated export line balances
        export_rows = db.execute(
            select(m.Balance)
            .join(m.ExportLine, m.ExportLine.id == m.Balance.ref_id)
            .where(m.Balance.side == "export_line", m.ExportLine.entity_id == ent.id)
            .order_by(m.Balance.id)
        ).all()
        for (bal,) in export_rows:
            if bal.available > ZERO:
                export = db.get(m.ExportLine, bal.ref_id)
                unallocated.append(
                    {
                        "kind": "unallocated_export_amount",
                        "entity_code": ent.code,
                        "export_line_id": export.id,
                        "declaration_no": export.declaration_no,
                        "currency": export.currency,
                        "declared_amount": str(bal.total),
                        "unallocated_amount": str(bal.available),
                        "reason": "no confirmed receipt evidence covers this export line balance",
                    }
                )

        # difference cases
        for case in db.scalars(
            select(m.DifferenceCase)
            .where(m.DifferenceCase.entity_id == ent.id, m.DifferenceCase.state == "open")
            .order_by(m.DifferenceCase.id)
        ).all():
            differences.append(
                {
                    "difference_case_id": case.id,
                    "kind": case.kind,
                    "entity_code": ent.code,
                    "ref_type": case.ref_type,
                    "ref_id": case.ref_id,
                    "detail": case.detail,
                    "state": case.state,
                }
            )

        # frozen confirmed lines — explicitly excluded from confirmed received amount
        frozen = db.scalars(
            select(m.ReconciliationLine).where(
                m.ReconciliationLine.entity_id == ent.id,
                m.ReconciliationLine.status == "needs_review",
            )
        ).all()
        for line in frozen:
            differences.append(
                {
                    "difference_case_id": None,
                    "kind": "confirmed_line_under_review",
                    "entity_code": ent.code,
                    "ref_type": "reconciliation_line",
                    "ref_id": line.id,
                    "detail": {
                        "amount": str(line.amount),
                        "frozen_at": line.frozen_at,
                        "reason": line.frozen_reason,
                        "effect": "excluded from confirmed received amount; occupancy stays frozen",
                    },
                    "state": "open",
                }
            )

        # receipt order conflicts
        for tax in db.scalars(
            select(m.TaxLine).where(m.TaxLine.entity_id == ent.id, m.TaxLine.order_conflict.is_(True))
        ).all():
            differences.append(
                {
                    "difference_case_id": None,
                    "kind": "receipt_order_conflict",
                    "entity_code": ent.code,
                    "ref_type": "tax_line",
                    "ref_id": tax.id,
                    "detail": {
                        "batch_no": tax.batch_no,
                        "issue_time": tax.issue_time,
                        "sequence_no": tax.sequence_no,
                        "note": "issue time and verifiable sequence disagree; no state transition applied",
                    },
                    "state": "open",
                }
            )

        # reverse candidates
        for rev in db.scalars(
            select(m.ReverseCandidate).where(
                m.ReverseCandidate.entity_id == ent.id,
                m.ReverseCandidate.status == "candidate",
            )
        ).all():
            differences.append(
                {
                    "difference_case_id": None,
                    "kind": "reverse_quantity_candidate",
                    "entity_code": ent.code,
                    "ref_type": "export_line",
                    "ref_id": rev.export_line_id,
                    "detail": {
                        "reverse_candidate_id": rev.id,
                        "quantity": str(rev.quantity),
                        "currency": rev.currency,
                        "amount": str(rev.amount) if rev.amount is not None else None,
                        "unique_attribution": rev.unique_attribution,
                        "evidence_grade": rev.evidence_grade,
                        "reasons": rev.reasons,
                        "effect": "retained as reverse quantity candidate; no eligibility or quantity change",
                    },
                    "state": "open",
                }
            )

    run = m.CompileRun(
        entity_id=entity_id,
        run_by=actor,
        facts_count=len(facts),
        unallocated_count=len(unallocated),
        differences_count=len(differences),
        result=jsonable(
            {
                "facts": facts,
                "unallocated": unallocated,
                "differences": differences,
                "rule": "only confirmed, active reconciliation lines are published as facts; "
                "candidates and frozen lines are never counted as received",
            }
        ),
    )
    db.add(run)
    db.flush()
    return run


def _unallocated_reason(db: Session, txn: m.BankTransaction, bal: m.Balance, receipt) -> str:
    pending = db.scalar(
        select(func.count(m.ReconciliationCandidate.id)).where(
            m.ReconciliationCandidate.bank_transaction_id == txn.id,
            m.ReconciliationCandidate.status == "proposed",
        )
    )
    if pending:
        return (
            "combined remittance without a unique batch key: constrained candidates await analyst "
            "confirmation; no proportional or per-line allocation was applied"
        )
    if receipt and receipt.state == "partial":
        return "partial receipt confirmed; remainder not linked to any confirmed evidence"
    if receipt and receipt.state == "conflict":
        return "receipt conflict open; remainder held pending resolution"
    return "no unique payment reference and no analyst-confirmed allocation"


def _open_candidate_briefs(db: Session, txn_id: int) -> list[dict]:
    rows = db.scalars(
        select(m.ReconciliationCandidate)
        .where(
            m.ReconciliationCandidate.bank_transaction_id == txn_id,
            m.ReconciliationCandidate.status == "proposed",
        )
        .order_by(m.ReconciliationCandidate.id)
    ).all()
    return [
        {
            "candidate_id": c.id,
            "export_line_id": c.export_line_id,
            "proposed_amount": str(c.proposed_amount),
            "evidence_grade": c.evidence_grade,
            "strategy": c.strategy,
        }
        for c in rows
    ]


def _fact_payload(db: Session, line: m.ReconciliationLine, txn, export) -> dict:
    dossier = None
    if export is not None:
        dossier = db.scalar(select(m.Dossier).where(m.Dossier.export_line_id == export.id))
    export_doc = None
    if export is not None and export.invoice_line_id:
        invoice_line = db.get(m.InvoiceLine, export.invoice_line_id)
        if invoice_line:
            export_doc = db.get(m.SourceDocument, invoice_line.document_id)

    return {
        "reconciliation_line_id": line.id,
        "entity_id": line.entity_id,
        "amount": str(line.amount),
        "currency": line.currency,
        "status": line.status,
        "strategy": line.strategy,
        "evidence_grade": line.evidence_grade,
        "rules": line.rules,
        "confirmed_by": line.confirmed_by,
        "confirmed_at": line.confirmed_at,
        "bank_transaction": {
            "id": txn.id,
            "txn_ref": txn.txn_ref,
            "amount": str(txn.amount),
            "value_date": txn.value_date,
            "remittance_info": txn.remittance_info,
            "source_document_id": txn.document_id,
        },
        "export_line": None
        if export is None
        else {
            "id": export.id,
            "declaration_no": export.declaration_no,
            "invoice_no": export.invoice_no,
            "declared_amount": str(export.declared_amount),
            "declared_quantity": str(export.declared_quantity),
            "currency": export.currency,
            "source_document_id": export_doc.id if export_doc else None,
            "source_location": export_doc.original_location if export_doc else None,
        },
        "dossier_id": dossier.id if dossier else None,
    }


def fact_detail(db: Session, line: m.ReconciliationLine) -> dict:
    """Full drilldown for one reconciliation line."""
    txn = db.get(m.BankTransaction, line.bank_transaction_id)
    export = db.get(m.ExportLine, line.export_line_id)
    bank_doc = db.get(m.SourceDocument, txn.document_id) if txn else None
    declaration = db.scalar(
        select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == line.export_line_id)
    )
    receipt = db.scalar(select(m.ReceiptLedger).where(m.ReceiptLedger.bank_transaction_id == line.bank_transaction_id))
    holds = db.scalars(
        select(m.BalanceHold)
        .where(m.BalanceHold.reconciliation_line_id == line.id)
        .order_by(m.BalanceHold.at, m.BalanceHold.id)
    ).all()
    balances = db.scalars(
        select(m.Balance).where(
            (m.Balance.side == "bank_transaction") & (m.Balance.ref_id == line.bank_transaction_id)
            | (m.Balance.side == "export_line") & (m.Balance.ref_id == line.export_line_id)
        )
    ).all()

    revision_chain = []
    if export is not None and export.customs_line_id:
        current = db.get(m.CustomsLine, export.customs_line_id)
        seen = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            revision_chain.append(
                {
                    "customs_line_id": current.id,
                    "declaration_no": current.declaration_no,
                    "line_no": current.line_no,
                    "revision_no": current.revision_no,
                    "amount": str(current.amount),
                    "quantity": str(current.quantity),
                    "is_current": current.is_current,
                    "changed_fields": current.changed_fields,
                    "source_document_id": current.document_id,
                }
            )
            current = db.get(m.CustomsLine, current.revises_id) if current.revises_id else None
        revision_chain.reverse()

    scenarios = []
    if txn is not None:
        from .resolver import constrained_candidates

        scenarios = constrained_candidates(db, txn)

    source_files = []
    for doc in [bank_doc]:
        if doc:
            source_files.append(_doc_brief(doc))
    if export is not None and export.invoice_line_id:
        inv = db.get(m.InvoiceLine, export.invoice_line_id)
        if inv:
            source_files.append(_doc_brief(db.get(m.SourceDocument, inv.document_id)))
    if export is not None and export.customs_line_id:
        c = db.get(m.CustomsLine, export.customs_line_id)
        if c:
            source_files.append(_doc_brief(db.get(m.SourceDocument, c.document_id)))
    if export is not None:
        for tax in db.scalars(select(m.TaxLine).where(m.TaxLine.export_line_id == export.id)).all():
            source_files.append(_doc_brief(db.get(m.SourceDocument, tax.document_id)))

    source_files = [s for s in source_files if s]

    return jsonable(
        {
            "reconciliation_line": {
                "id": line.id,
                "amount": line.amount,
                "currency": line.currency,
                "status": line.status,
                "strategy": line.strategy,
                "evidence_grade": line.evidence_grade,
                "rules": line.rules,
                "confirmed_by": line.confirmed_by,
                "confirmed_at": line.confirmed_at,
                "voided_at": line.voided_at,
                "void_reason": line.void_reason,
            },
            "source_files": source_files,
            "status_ledgers": {
                "declaration": None
                if declaration is None
                else {
                    "state": declaration.state,
                    "submitted_at": declaration.submitted_at,
                    "reviewed_at": declaration.reviewed_at,
                    "review_ref": declaration.review_ref,
                    "correction_open": declaration.correction_open,
                    "history": ledgers.declaration_history(db, line.export_line_id),
                },
                "receipt": None
                if receipt is None
                else {
                    "state": receipt.state,
                    "pending_allocation": receipt.pending_allocation,
                    "conflict_open": receipt.conflict_open,
                },
                "note": "declaration and receipt ledgers are separate; settlement facts live here",
            },
            "balance_occupancy": {
                "balances": [
                    {
                        "side": b.side,
                        "ref_id": b.ref_id,
                        "total": b.total,
                        "committed": b.committed,
                        "available": b.available,
                        "version": b.version,
                    }
                    for b in balances
                ],
                "holds": [
                    {
                        "id": h.id,
                        "side": h.side,
                        "amount": h.amount,
                        "action": h.action,
                        "actor": h.actor,
                        "at": h.at,
                        "note": h.note,
                    }
                    for h in holds
                ],
            },
            "allocation_scenarios": scenarios,
            "revision_chain": revision_chain,
            "judgement_rules": line.rules,
        }
    )


def _doc_brief(doc: m.SourceDocument | None) -> dict | None:
    if doc is None:
        return None
    return {
        "document_id": doc.id,
        "doc_type": doc.doc_type,
        "external_no": doc.external_no,
        "original_location": doc.original_location,
        "event_time": doc.event_time,
        "received_time": doc.received_time,
        "sha256": doc.sha256,
        "source_version": doc.source_version,
    }
