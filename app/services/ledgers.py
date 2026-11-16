"""Declaration status ledger and receipt status ledger.

The two ledgers are deliberately separate:
  * declaration_ledgers hold declaration review states (draft/submitted/verified/rejected/correction_pending)
  * receipt_ledgers hold remittance states (unmatched/pending_allocation/partial/matched/conflict)
Neither exposes a shared "settled" column — settlement facts live in reconciliation_lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..util import jsonable

DECLARATION_STATES = {"draft", "submitted", "verified", "rejected", "correction_pending"}
RECEIPT_STATES = {"unmatched", "pending_allocation", "partial", "matched", "conflict"}

_DECL_TRANSITIONS = {
    ("draft", "submitted"),
    ("submitted", "verified"),
    ("submitted", "rejected"),
    ("submitted", "correction_pending"),
    ("verified", "correction_pending"),
    ("rejected", "draft"),
    ("rejected", "submitted"),
    ("correction_pending", "submitted"),
    ("correction_pending", "verified"),
    ("correction_pending", "rejected"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def apply_declaration_event(
    db: Session,
    export: m.ExportLine,
    event: str,
    *,
    actor: str,
    at: datetime | None = None,
    reference: str | None = None,
    note: str | None = None,
    source_document_id: int | None = None,
) -> m.DeclarationLedger:
    ledger = db.scalar(
        select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export.id).with_for_update()
    )
    if ledger is None:
        ledger = m.DeclarationLedger(export_line_id=export.id, state="draft")
        db.add(ledger)
        db.flush()

    target = {
        "draft_recorded": "draft",
        "submitted": "submitted",
        "verified": "verified",
        "rejected": "rejected",
    }[event]

    if (ledger.state, target) not in _DECL_TRANSITIONS and ledger.state != target:
        raise HTTPException(
            409, f"declaration transition not allowed: {ledger.state} -> {target} (export line {export.id})"
        )
    if ledger.correction_open and target == "submitted":
        # re-submission after a correction requires the corrected customs line to be current
        customs = db.get(m.CustomsLine, export.customs_line_id) if export.customs_line_id else None
        if customs is None or not customs.is_current:
            raise HTTPException(
                409,
                f"cannot re-submit export line {export.id}: correction chain has no current customs line",
            )
    if ledger.correction_open and target in ("verified", "draft"):
        # a review verdict or a withdrawn draft closes the correction cycle
        ledger.correction_open = False

    prev = ledger.state
    ledger.state = target
    when = at or _now()
    if event == "submitted":
        ledger.submitted_at = when
        # verdict of the previous review remains queryable through declaration_events
        ledger.reviewed_at = None
        ledger.review_ref = reference
    elif event == "verified":
        ledger.reviewed_at = when
        ledger.review_ref = reference
    elif event == "rejected":
        ledger.reviewed_at = when
        ledger.review_ref = reference
        ledger.review_note = note
    ledger.version += 1
    ledger.updated_at = when
    db.add(
        m.DeclarationEvent(
            export_line_id=export.id,
            from_state=prev,
            to_state=target,
            at=when,
            actor=actor,
            reason=note,
            source_document_id=source_document_id,
            payload=jsonable({"reference": reference}),
        )
    )
    db.flush()
    return ledger


def refresh_receipt_state(db: Session, txn: m.BankTransaction) -> m.ReceiptLedger:
    """Derive receipt state from confirmed, non-voided reconciliation lines + open candidates.

    Never auto-transitions to matched based on proposed candidates.
    """
    ledger = db.scalar(
        select(m.ReceiptLedger).where(m.ReceiptLedger.bank_transaction_id == txn.id).with_for_update()
    )
    if ledger is None:
        ledger = m.ReceiptLedger(bank_transaction_id=txn.id, state="unmatched")
        db.add(ledger)
        db.flush()

    lines = db.scalars(
        select(m.ReconciliationLine).where(
            m.ReconciliationLine.bank_transaction_id == txn.id,
            m.ReconciliationLine.status.in_(("active", "needs_review")),
        )
    ).all()
    committed = sum((l.amount for l in lines if l.status == "active"), Decimal("0"))
    pending = db.scalar(
        select(m.ReconciliationCandidate.id)
        .where(
            m.ReconciliationCandidate.bank_transaction_id == txn.id,
            m.ReconciliationCandidate.status == "proposed",
        )
        .limit(1)
    )
    conflicts = db.scalar(
        select(m.DifferenceCase.id)
        .where(
            m.DifferenceCase.kind.in_(("receipt_order_conflict", "receipt_conflict")),
            m.DifferenceCase.ref_type == "bank_transaction",
            m.DifferenceCase.ref_id == txn.id,
            m.DifferenceCase.state == "open",
        )
        .limit(1)
    )
    # A rejected over-allocation attempt is published as a difference case (see balance.py); it does
    # not make the money itself ambiguous, so it is deliberately not part of this query.
    frozen = any(l.status == "needs_review" for l in lines)

    if conflicts:
        state = "conflict"
    elif committed == 0 and not pending:
        state = "unmatched"
    elif committed == 0:
        state = "pending_allocation"
    elif committed < txn.amount or frozen:
        state = "partial"
    elif committed == txn.amount:
        state = "matched"
    else:
        state = "conflict"

    if ledger.state != state:
        prev = ledger.state
        ledger.state = state
        ledger.version += 1
        ledger.updated_at = _now()
        db.add(
            m.ReceiptEvent(
                bank_transaction_id=txn.id,
                from_state=prev,
                to_state=state,
                reason="derived from reconciliation lines and open candidates",
                payload=jsonable(
                    {
                        "committed": committed,
                        "amount": txn.amount,
                        "pending_candidates": bool(pending),
                        "frozen_lines": frozen,
                    }
                ),
            )
        )
    ledger.pending_allocation = bool(pending) and committed < txn.amount
    # A rejected over-allocation *attempt* is already published as a difference case; it does not
    # make the money itself ambiguous, so conflict_open tracks only genuine receipt conflicts.
    ledger.conflict_open = bool(conflicts)
    db.flush()
    return ledger


def add_receipt_conflict(db: Session, txn: m.BankTransaction, kind: str, detail: dict) -> m.DifferenceCase:
    case = db.scalar(
        select(m.DifferenceCase).where(
            m.DifferenceCase.kind == kind,
            m.DifferenceCase.ref_type == "bank_transaction",
            m.DifferenceCase.ref_id == txn.id,
            m.DifferenceCase.state == "open",
        )
    )
    if case is None:
        case = m.DifferenceCase(
            entity_id=txn.entity_id,
            kind=kind,
            state="open",
            ref_type="bank_transaction",
            ref_id=txn.id,
            detail=jsonable(detail),
        )
        db.add(case)
        db.flush()
    else:
        case.detail = jsonable(detail)
    return case


def declaration_history(db: Session, export_line_id: int) -> list[dict]:
    rows = db.scalars(
        select(m.DeclarationEvent)
        .where(m.DeclarationEvent.export_line_id == export_line_id)
        .order_by(m.DeclarationEvent.at, m.DeclarationEvent.id)
    ).all()
    return [
        {
            "id": r.id,
            "from_state": r.from_state,
            "to_state": r.to_state,
            "at": r.at,
            "actor": r.actor,
            "reason": r.reason,
            "source_document_id": r.source_document_id,
            "payload": r.payload,
        }
        for r in rows
    ]
