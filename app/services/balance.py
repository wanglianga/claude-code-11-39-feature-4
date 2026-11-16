"""Balance controller: atomic occupancy of bank and export-line balances.

Rules enforced here:
  * Candidates never consume balances; only confirmed reconciliation lines do.
  * Every confirmation locks both balance rows (deterministic order) and verifies
    remaining availability on both sides — cumulative confirmations can never exceed
    the actual amount on either the bank transaction or the export line.
  * Combined remittances without a unique batch key only ever produce candidates;
    no proportional or per-line-count allocation is ever performed.
  * A confirmation entering review keeps its occupancy frozen (committed unchanged)
    but is excluded from compiled facts until voided or re-assigned, which release or
    transfer the occupancy with an audit record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from .. import models as m
from ..util import jsonable
from . import ledgers
from .resolver import build_export_dossier, constrained_candidates, resolve_all, unique_targets

ZERO = Decimal("0.00")


class OverAllocationError(Exception):
    """Raised when a confirmation would exceed available balance on either side."""

    def __init__(self, detail: dict):
        super().__init__("over_allocation")
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_balances(db: Session, refs: set[tuple[str, int]]) -> dict[tuple[str, int], m.Balance]:
    rows = db.scalars(
        select(m.Balance)
        .where(tuple_(m.Balance.side, m.Balance.ref_id).in_(list(refs)))
        .order_by(m.Balance.side, m.Balance.ref_id)
        .with_for_update()
    ).all()
    found = {(r.side, r.ref_id): r for r in rows}
    missing = refs - set(found)
    if missing:
        raise HTTPException(409, f"balance rows missing: {sorted(missing)}")
    return found


def _move(bal: m.Balance, delta_committed: Decimal, delta_available: Decimal) -> None:
    bal.committed += delta_committed
    bal.available += delta_available
    bal.version += 1
    if bal.committed < 0 or bal.available < 0 or bal.committed + bal.available != bal.total:
        raise HTTPException(409, f"balance invariant violated on {bal.side}:{bal.ref_id}")


def resettle_export_balance(db: Session, export_line_id: int) -> None:
    """Re-anchor an export-line balance ceiling after occupancy changes.

    The ceiling is max(declared amount, committed amount): a customs correction that lowers the
    declared amount below already-frozen occupancy never rewrites that occupancy; the ceiling stays
    at the committed amount (available = 0) until the frozen lines are voided or reassigned, and then
    shrinks back to the declared amount.
    """
    bal = db.scalar(
        select(m.Balance).where(m.Balance.side == "export_line", m.Balance.ref_id == export_line_id).with_for_update()
    )
    if bal is None:
        return
    export = db.get(m.ExportLine, export_line_id)
    declared = export.declared_amount if export is not None else bal.total
    bal.total = max(declared, bal.committed)
    bal.available = bal.total - bal.committed
    bal.version += 1
    db.flush()


def _hold(
    db: Session,
    bal: m.Balance,
    line_id: int,
    amount: Decimal,
    action: str,
    actor: str,
    note: str | None = None,
):
    db.add(
        m.BalanceHold(
            balance_id=bal.id,
            side=bal.side,
            ref_id=bal.ref_id,
            reconciliation_line_id=line_id,
            amount=amount,
            action=action,
            actor=actor,
            note=note,
        )
    )


def confirm_candidate(
    db: Session,
    *,
    actor: str,
    candidate_id: int,
    amount: Decimal | None = None,
    note: str | None = None,
) -> m.ReconciliationLine:
    candidate = db.scalar(
        select(m.ReconciliationCandidate)
        .where(m.ReconciliationCandidate.id == candidate_id)
        .with_for_update()
    )
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    if candidate.status != "proposed":
        raise HTTPException(409, f"candidate {candidate.id} is {candidate.status}, not proposed")

    confirm_amount = amount or candidate.proposed_amount
    if confirm_amount <= 0:
        raise HTTPException(422, "confirmation amount must be positive")
    if confirm_amount > candidate.proposed_amount:
        raise HTTPException(
            409,
            f"confirmation amount {confirm_amount} exceeds candidate proposal {candidate.proposed_amount}",
        )

    txn = db.get(m.BankTransaction, candidate.bank_transaction_id)
    export = db.get(m.ExportLine, candidate.export_line_id)
    if txn is None or export is None:
        raise HTTPException(409, "candidate references missing bank transaction or export line")
    if txn.entity_id != export.entity_id:
        raise HTTPException(403, "cross-entity confirmation is not permitted")

    bals = _lock_balances(
        db, {("bank_transaction", txn.id), ("export_line", export.id)}
    )
    bank_bal = bals[("bank_transaction", txn.id)]
    export_bal = bals[("export_line", export.id)]
    if confirm_amount > bank_bal.available or confirm_amount > export_bal.available:
        ledgers.add_receipt_conflict(
            db,
            txn,
            "over_allocation_attempt",
            {
                "candidate_id": candidate.id,
                "requested": confirm_amount,
                "bank_available": bank_bal.available,
                "export_available": export_bal.available,
                "actor": actor,
                "rule": "cumulative confirmed amount may not exceed either side's actual amount",
            },
        )
        ledgers.refresh_receipt_state(db, txn)
        db.flush()
        raise OverAllocationError(
            {
                "error": "over_allocation",
                "bank_available": str(bank_bal.available),
                "export_available": str(export_bal.available),
                "requested": str(confirm_amount),
            }
        )

    _move(bank_bal, confirm_amount, -confirm_amount)
    _move(export_bal, confirm_amount, -confirm_amount)

    rules = [
        f"strategy:{candidate.strategy}",
        "evidence_grade:" + candidate.evidence_grade,
        "bank_available_checked",
        "export_line_available_checked",
        "amount<=min(bank_remaining, export_remaining)",
    ]
    if candidate.strategy == "unique_reference":
        rules.append("unique payment reference bound to a single export line")
    else:
        rules.append("analyst-confirmed constrained allocation candidate (never auto-derived)")

    line = m.ReconciliationLine(
        entity_id=txn.entity_id,
        bank_transaction_id=txn.id,
        export_line_id=export.id,
        candidate_id=candidate.id,
        amount=confirm_amount,
        currency=txn.currency,
        status="active",
        evidence_grade=candidate.evidence_grade,
        strategy=candidate.strategy,
        rules=rules,
        confirmed_by=actor,
    )
    db.add(line)
    db.flush()

    _hold(db, bank_bal, line.id, confirm_amount, "occupy", actor, note)
    _hold(db, export_bal, line.id, confirm_amount, "occupy", actor, note)

    candidate.status = "confirmed"
    candidate.decided_at = _now()
    candidate.decided_by = actor
    if confirm_amount < candidate.proposed_amount:
        residual = candidate.proposed_amount - confirm_amount
        db.add(
            m.ReconciliationCandidate(
                entity_id=candidate.entity_id,
                bank_transaction_id=candidate.bank_transaction_id,
                export_line_id=candidate.export_line_id,
                strategy=candidate.strategy,
                evidence_grade=candidate.evidence_grade,
                proposed_amount=residual,
                currency=candidate.currency,
                status="proposed",
                reasons=jsonable(["residual after partial confirmation"]),
                created_by=actor,
            )
        )

    db.add(
        m.AuditLog(
            entity_id=txn.entity_id,
            actor=actor,
            action="reconciliation.confirm",
            object_type="reconciliation_line",
            object_id=line.id,
            payload=jsonable(
                {
                    "candidate_id": candidate.id,
                    "bank_transaction_id": txn.id,
                    "export_line_id": export.id,
                    "amount": confirm_amount,
                    "strategy": candidate.strategy,
                    "note": note,
                }
            ),
        )
    )
    ledgers.refresh_receipt_state(db, txn)
    db.flush()
    return line


def auto_confirm_unique(db: Session, *, entity_id: int) -> list[int]:
    """First pass: confirm unique payment-reference links for one entity.

    Only line-unique references are ever auto-confirmed. Amount is capped by both
    sides' remaining available balance; residual stays unallocated.

    Each transaction is confirmed inside its own savepoint, so one blocked transaction can be
    withdrawn without rolling back the successes already made in this pass.
    """
    txns = db.scalars(
        select(m.BankTransaction).where(m.BankTransaction.entity_id == entity_id).order_by(m.BankTransaction.id)
    ).all()
    confirmed: list[int] = []
    for txn in txns:
        has_active = db.scalar(
            select(m.ReconciliationLine.id)
            .where(
                m.ReconciliationLine.bank_transaction_id == txn.id,
                m.ReconciliationLine.status.in_(("active", "needs_review")),
            )
            .limit(1)
        )
        if has_active:
            continue
        targets = unique_targets(db, txn)
        if len(targets) != 1:
            continue
        export_line_id = targets[0].id
        savepoint = db.begin_nested()
        try:
            candidate = db.scalar(
                select(m.ReconciliationCandidate)
                .where(
                    m.ReconciliationCandidate.bank_transaction_id == txn.id,
                    m.ReconciliationCandidate.export_line_id == export_line_id,
                    m.ReconciliationCandidate.status == "proposed",
                )
                .limit(1)
            )
            if candidate is None:
                bank_bal = db.scalar(
                    select(m.Balance)
                    .where(m.Balance.side == "bank_transaction", m.Balance.ref_id == txn.id)
                )
                export_bal = db.scalar(
                    select(m.Balance)
                    .where(m.Balance.side == "export_line", m.Balance.ref_id == export_line_id)
                )
                available = min(bank_bal.available, export_bal.available)
                if available <= 0:
                    savepoint.rollback()
                    _record_blocked_unique_link(db, entity_id, txn.id, export_line_id, None)
                    db.flush()
                    continue
                candidate = m.ReconciliationCandidate(
                    entity_id=entity_id,
                    bank_transaction_id=txn.id,
                    export_line_id=export_line_id,
                    strategy="unique_reference",
                    evidence_grade="A",
                    proposed_amount=available,
                    currency=txn.currency,
                    status="proposed",
                    reasons=jsonable(
                        [
                            "unique line-level payment reference",
                            f"capped by available balances "
                            f"(bank {bank_bal.available}, export {export_bal.available})",
                        ]
                    ),
                    created_by="system:resolver",
                )
                db.add(candidate)
                db.flush()
            candidate_id = candidate.id
            line = confirm_candidate(
                db,
                actor="system:auto_unique",
                candidate_id=candidate_id,
                note="unique reference auto-matched (low review)",
            )
            line_id = line.id
            savepoint.commit()
            confirmed.append(line_id)
        except OverAllocationError:
            # never part-apply: the savepoint undoes this transaction's candidate + conflict case only.
            # The unconfirmed amount stays in the unallocated balance and is published as a
            # difference reason; an analyst can still create a fresh partial candidate.
            savepoint.rollback()
            _record_blocked_unique_link(db, entity_id, txn.id, export_line_id, None)
            db.flush()
            continue
        except Exception:
            savepoint.rollback()
            raise
    return confirmed


def _record_blocked_unique_link(
    db: Session, entity_id: int, txn_id: int, export_line_id: int, candidate_id: int | None
) -> None:
    existing = db.scalar(
        select(m.DifferenceCase.id).where(
            m.DifferenceCase.kind == "unique_link_blocked_by_balance",
            m.DifferenceCase.ref_type == "bank_transaction",
            m.DifferenceCase.ref_id == txn_id,
            m.DifferenceCase.state == "open",
        )
    )
    if existing:
        return
    db.add(
        m.DifferenceCase(
            entity_id=entity_id,
            kind="unique_link_blocked_by_balance",
            state="open",
            ref_type="bank_transaction",
            ref_id=txn_id,
            detail=jsonable(
                {
                    "candidate_id": candidate_id,
                    "export_line_id": export_line_id,
                    "rule": "the unique link could not be confirmed because the bank side or the export "
                    "line has no available amount left; the remainder stays unallocated",
                }
            ),
        )
    )


def generate_candidates(db: Session, txn: m.BankTransaction, actor: str) -> list[m.ReconciliationCandidate]:
    """Second pass: constrained candidates for combined remittances (manual confirmation only)."""
    if unique_targets(db, txn):
        return []
    existing = {
        c.export_line_id
        for c in db.scalars(
            select(m.ReconciliationCandidate).where(
                m.ReconciliationCandidate.bank_transaction_id == txn.id,
                m.ReconciliationCandidate.status == "proposed",
            )
        ).all()
    }
    out: list[m.ReconciliationCandidate] = []
    for entry in constrained_candidates(db, txn):
        if entry["export_line_id"] in existing:
            continue
        cand = m.ReconciliationCandidate(
            entity_id=txn.entity_id,
            bank_transaction_id=txn.id,
            export_line_id=entry["export_line_id"],
            strategy="constrained_allocation",
            evidence_grade=entry["evidence_grade"],
            proposed_amount=Decimal(entry["proposed_amount"]),
            currency=entry["currency"],
            status="proposed",
            reasons=jsonable(entry["reasons"] + ["requires analyst confirmation; not proportionally allocated"]),
            created_by=actor,
        )
        db.add(cand)
        out.append(cand)
    db.flush()
    if out:
        ledgers.refresh_receipt_state(db, txn)
    return out


def run_reconciliation_pass(db: Session, *, entity_id: int, actor: str) -> dict:
    """Unique links first, then constrained candidates for combined remittances."""
    resolve_all(db, entity_id)
    auto = auto_confirm_unique(db, entity_id=entity_id)
    txns = db.scalars(
        select(m.BankTransaction).where(m.BankTransaction.entity_id == entity_id).order_by(m.BankTransaction.id)
    ).all()
    created = 0
    for txn in txns:
        created += len(generate_candidates(db, txn, actor))
        ledgers.refresh_receipt_state(db, txn)
    return {"auto_confirmed": auto, "candidates_created": created}


def flag_lines_for_review(
    db: Session,
    *,
    export_line_id: int | None = None,
    bank_transaction_id: int | None = None,
    line_ids: list[int] | None = None,
    reason: str,
    kind: str = "confirmed_link_needs_review",
) -> int:
    """Freeze confirmed lines (occupancy stays committed) and exclude from compiled facts."""
    stmt = select(m.ReconciliationLine).where(m.ReconciliationLine.status == "active")
    if line_ids is not None:
        stmt = stmt.where(m.ReconciliationLine.id.in_(line_ids))
    if export_line_id is not None:
        stmt = stmt.where(m.ReconciliationLine.export_line_id == export_line_id)
    if bank_transaction_id is not None:
        stmt = stmt.where(m.ReconciliationLine.bank_transaction_id == bank_transaction_id)
    lines = db.scalars(stmt.with_for_update()).all()
    for line in lines:
        line.status = "needs_review"
        line.frozen_at = _now()
        line.frozen_reason = reason
        db.add(
            m.AuditLog(
                entity_id=line.entity_id,
                actor="system",
                action="reconciliation.freeze",
                object_type="reconciliation_line",
                object_id=line.id,
                payload=jsonable({"reason": reason, "occupancy": "frozen, not released"}),
            )
        )
    if lines:
        db.add(
            m.DifferenceCase(
                entity_id=lines[0].entity_id,
                kind=kind,
                state="open",
                ref_type="export_line" if export_line_id else "bank_transaction",
                ref_id=export_line_id or bank_transaction_id,
                detail=jsonable(
                    {
                        "reason": reason,
                        "reconciliation_line_ids": [l.id for l in lines],
                        "effect": "excluded from confirmed received amount on next compile; balances frozen",
                    }
                ),
            )
        )
        db.flush()
    return len(lines)


def void_line(db: Session, *, actor: str, line_id: int, reason: str) -> m.ReconciliationLine:
    line = db.scalar(
        select(m.ReconciliationLine).where(m.ReconciliationLine.id == line_id).with_for_update()
    )
    if line is None:
        raise HTTPException(404, "reconciliation line not found")
    if line.status not in ("active", "needs_review"):
        raise HTTPException(409, f"reconciliation line {line.id} is {line.status}")

    bals = _lock_balances(
        db, {("bank_transaction", line.bank_transaction_id), ("export_line", line.export_line_id)}
    )
    bank_bal = bals[("bank_transaction", line.bank_transaction_id)]
    export_bal = bals[("export_line", line.export_line_id)]
    _move(bank_bal, -line.amount, line.amount)
    _move(export_bal, -line.amount, line.amount)
    _hold(db, bank_bal, line.id, line.amount, "release", actor, reason)
    _hold(db, export_bal, line.id, line.amount, "release", actor, reason)
    prev_status = line.status
    line.status = "voided"
    line.voided_at = _now()
    line.voided_by = actor
    line.void_reason = reason
    db.flush()
    resettle_export_balance(db, line.export_line_id)
    db.add(
        m.AuditLog(
            entity_id=line.entity_id,
            actor=actor,
            action="reconciliation.void",
            object_type="reconciliation_line",
            object_id=line.id,
            payload=jsonable({"reason": reason, "previous_status": prev_status, "amount": line.amount}),
        )
    )
    txn = db.get(m.BankTransaction, line.bank_transaction_id)
    ledgers.refresh_receipt_state(db, txn)
    db.flush()
    return line


def reassign_line(
    db: Session, *, actor: str, line_id: int, target_export_line_id: int, reason: str
) -> m.ReconciliationLine:
    """Transfer frozen/confirmed occupancy to the correct export line (atomic)."""
    old = db.scalar(
        select(m.ReconciliationLine).where(m.ReconciliationLine.id == line_id).with_for_update()
    )
    if old is None:
        raise HTTPException(404, "reconciliation line not found")
    if old.status not in ("active", "needs_review"):
        raise HTTPException(409, f"reconciliation line {old.id} is {old.status}, cannot reassign")
    target = db.get(m.ExportLine, target_export_line_id)
    if target is None:
        raise HTTPException(404, "target export line not found")
    if target.entity_id != old.entity_id:
        raise HTTPException(403, "cross-entity reassignment is not permitted")
    if target.id == old.export_line_id:
        raise HTTPException(422, "target export line is the current one")

    bals = _lock_balances(
        db,
        {
            ("bank_transaction", old.bank_transaction_id),
            ("export_line", old.export_line_id),
            ("export_line", target.id),
        },
    )
    old_bal = bals[("export_line", old.export_line_id)]
    new_bal = bals[("export_line", target.id)]
    if old.amount > new_bal.available:
        raise HTTPException(
            409,
            f"target export line {target.id} has only {new_bal.available} available, need {old.amount}",
        )
    _move(old_bal, -old.amount, old.amount)
    _move(new_bal, old.amount, -old.amount)
    _hold(db, old_bal, old.id, old.amount, "transfer_out", actor, reason)
    _hold(db, new_bal, old.id, old.amount, "transfer_in", actor, reason)

    new = m.ReconciliationLine(
        entity_id=old.entity_id,
        bank_transaction_id=old.bank_transaction_id,
        export_line_id=target.id,
        candidate_id=old.candidate_id,
        amount=old.amount,
        currency=old.currency,
        status="active",
        evidence_grade=old.evidence_grade,
        strategy="reassigned",
        rules=old.rules + [f"reassigned from reconciliation line {old.id}: {reason}"],
        confirmed_by=actor,
    )
    db.add(new)
    db.flush()
    _hold(db, bals[("bank_transaction", old.bank_transaction_id)], new.id, old.amount, "transfer_keep", actor, reason)

    old.status = "reassigned"
    old.superseded_by_id = new.id
    old.voided_at = _now()
    old.voided_by = actor
    old.void_reason = reason
    db.flush()
    # the old line's ceiling may fall back to its declared amount once occupancy has moved away
    resettle_export_balance(db, old.export_line_id)
    db.add(
        m.AuditLog(
            entity_id=old.entity_id,
            actor=actor,
            action="reconciliation.reassign",
            object_type="reconciliation_line",
            object_id=old.id,
            payload=jsonable(
                {
                    "target_export_line_id": target.id,
                    "new_line_id": new.id,
                    "amount": old.amount,
                    "reason": reason,
                }
            ),
        )
    )
    txn = db.get(m.BankTransaction, old.bank_transaction_id)
    ledgers.refresh_receipt_state(db, txn)
    build_export_dossier(db, target)
    db.flush()
    return new
