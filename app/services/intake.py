"""Intake ledger ingestion: source documents and their lines (append-only)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..schemas import IntakeLine, IntakeRequest
from ..util import jsonable, sha256_of


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _entity(db: Session, code: str | None) -> m.Entity | None:
    if not code:
        return None
    ent = db.scalar(select(m.Entity).where(m.Entity.code == code))
    if ent is None:
        raise HTTPException(422, f"entity_code not found: {code}")
    return ent


def _resolve_contract(db: Session, entity: m.Entity | None, no: str | None) -> m.Contract | None:
    if not entity or not no:
        return None
    return db.scalar(
        select(m.Contract).where(m.Contract.entity_id == entity.id, m.Contract.contract_no == no)
    )


def ingest_document(db: Session, req: IntakeRequest, actor: str) -> dict:
    entity = _entity(db, req.entity_code)
    received = req.received_time or _utcnow()
    payload_hash = req.sha256 or sha256_of(req.model_dump(mode="json"))

    doc = m.SourceDocument(
        doc_type=req.doc_type,
        entity_id=entity.id if entity else None,
        external_no=req.external_no,
        original_location=req.original_location,
        event_time=req.event_time,
        received_time=received,
        currency=req.currency.upper() if req.currency else None,
        amount=req.amount,
        quantity=req.quantity,
        sha256=payload_hash,
        source_version=req.source_version,
        supersedes_id=req.supersedes_document_id,
        meta=jsonable(req.meta),
        ingested_by=actor,
    )
    db.add(doc)
    db.flush()

    created: dict[str, list[int]] = {
        "invoice_lines": [],
        "customs_lines": [],
        "tax_lines": [],
        "bank_transactions": [],
        "packing_lines": [],
    }

    if req.doc_type == "invoice":
        created["invoice_lines"] = _ingest_invoice(db, entity, doc, req)
    elif req.doc_type in ("customs_archive", "customs_correction"):
        created["customs_lines"] = _ingest_customs(db, entity, doc, req)
    elif req.doc_type == "packing_list":
        created["packing_lines"] = _ingest_packing(db, entity, doc, req)
    elif req.doc_type == "tax_receipt":
        created["tax_lines"] = _ingest_tax(db, entity, doc, req)
    elif req.doc_type == "bank_statement":
        created["bank_transactions"] = _ingest_bank(db, entity, doc, req)
    elif req.doc_type == "entity_directory":
        _ingest_entity_directory(db, doc, req)
    elif req.doc_type == "contract":
        _ingest_contract(db, entity, doc, req)
    elif req.doc_type == "return_document":
        _ingest_return(db, entity, doc, req)

    db.add(
        m.AuditLog(
            entity_id=entity.id if entity else None,
            actor=actor,
            action="intake.document",
            object_type="source_document",
            object_id=doc.id,
            payload=jsonable({"doc_type": req.doc_type, "external_no": req.external_no, "created": created}),
        )
    )
    db.flush()
    return {"document_id": doc.id, "sha256": doc.sha256, "created": created}


def _ingest_invoice(db: Session, entity, doc, req) -> list[int]:
    if entity is None:
        raise HTTPException(422, "invoice requires entity_code")
    if not req.lines:
        raise HTTPException(422, "invoice requires lines")
    ids = []
    for idx, line in enumerate(req.lines, start=1):
        if line.quantity is None or line.amount is None or line.currency is None:
            raise HTTPException(422, f"invoice line {idx}: quantity, amount, currency required")
        contract = _resolve_contract(db, entity, line.contract_no)
        obj = m.InvoiceLine(
            document_id=doc.id,
            entity_id=entity.id,
            line_no=line.line_no or idx,
            sku=line.sku,
            description=line.description,
            quantity=line.quantity,
            amount=line.amount,
            currency=line.currency,
            contract_id=contract.id if contract else None,
        )
        db.add(obj)
        db.flush()
        ids.append(obj.id)
        _ensure_export_line(db, entity, invoice_line=obj, contract=contract, req_line=line)
    return ids


def _ensure_export_line(db: Session, entity, *, invoice_line=None, customs_line=None, contract=None, req_line=None):
    """Build the canonical export line for an invoice line and keep it in sync."""
    if invoice_line is not None:
        existing = db.scalar(select(m.ExportLine).where(m.ExportLine.invoice_line_id == invoice_line.id))
        if existing:
            return existing
        export_date = req_line.export_date if req_line else None
        obj = m.ExportLine(
            entity_id=entity.id,
            contract_id=contract.id if contract else None,
            invoice_line_id=invoice_line.id,
            invoice_no=req_line.invoice_no if req_line else None,
            declaration_no=req_line.declaration_no if req_line else None,
            sku=invoice_line.sku,
            description=invoice_line.description,
            currency=invoice_line.currency,
            declared_quantity=invoice_line.quantity,
            declared_amount=invoice_line.amount,
            export_date=export_date,
            evidence_grade="C",
        )
        db.add(obj)
        db.flush()
        _ensure_balance(db, "export_line", obj.id, obj.currency, obj.declared_amount)
        db.add(
            m.DeclarationLedger(export_line_id=obj.id, state="draft"),
        )
        db.flush()
        return obj
    return None


def _ingest_customs(db: Session, entity, doc, req) -> list[int]:
    if entity is None:
        raise HTTPException(422, "customs document requires entity_code")
    if not req.lines:
        raise HTTPException(422, "customs document requires lines")
    links: dict[int, int] = {}
    for k, v in (doc.meta or {}).get("line_links", {}).items():
        try:
            links[int(k)] = int(v)
        except (TypeError, ValueError):
            continue

    ids = []
    for idx, line in enumerate(req.lines, start=1):
        if line.amount is None or line.currency is None or line.quantity is None:
            raise HTTPException(422, f"customs line {idx}: quantity, amount, currency required")
        if req.doc_type == "customs_correction" and line.revises_line_no is None:
            raise HTTPException(
                422,
                f"customs correction line {idx}: revises_line_no is required — a correction must point at "
                "the current goods line it replaces rather than silently re-baseline the declaration",
            )
        line_no = line.line_no or idx
        customs = m.CustomsLine(
            document_id=doc.id,
            entity_id=entity.id,
            line_no=line_no,
            hs_code=line.hs_code,
            sku=line.sku,
            description=line.description,
            quantity=line.quantity,
            amount=line.amount,
            currency=line.currency,
            declaration_no=line.declaration_no,
            is_current=True,
        )
        db.add(customs)
        db.flush()

        if line.revises_line_no is not None:
            previous = db.scalar(
                select(m.CustomsLine).where(
                    m.CustomsLine.entity_id == entity.id,
                    m.CustomsLine.declaration_no == line.declaration_no,
                    m.CustomsLine.line_no == line.revises_line_no,
                    m.CustomsLine.is_current.is_(True),
                )
            )
            if previous is None:
                raise HTTPException(
                    422,
                    f"customs correction references unknown current line {line.revises_line_no} "
                    f"for declaration {line.declaration_no}",
                )
            customs.revises_id = previous.id
            customs.revision_no = previous.revision_no + 1
            previous.is_current = False

        # bind to the canonical export line: explicit line_links wins, else declaration+sku/description
        export = None
        forced = links.get(line_no)
        if forced:
            export = db.get(m.ExportLine, forced)
            if export is None or export.entity_id != entity.id:
                raise HTTPException(422, f"line_links references export line {forced} outside entity scope")
        else:
            export = _current_export_for_customs(db, entity, customs)

        if line.revises_line_no is not None:
            _apply_correction(db, customs, export)
        elif export is not None:
            _sync_export_from_customs(db, export, customs)

        ids.append(customs.id)

    db.add(
        m.AuditLog(
            entity_id=entity.id,
            actor=doc.ingested_by or "system",
            action="customs.ingest",
            object_type="source_document",
            object_id=doc.id,
            payload=jsonable({"lines": ids, "links": links, "is_correction": req.doc_type == "customs_correction"}),
        )
    )
    return ids


def _current_export_for_customs(db: Session, entity: m.Entity, customs: m.CustomsLine) -> m.ExportLine | None:
    """Find the export line by declaration + sku (preferred) or unique declaration + description."""
    stmt = select(m.ExportLine).where(
        m.ExportLine.entity_id == entity.id,
        m.ExportLine.declaration_no == customs.declaration_no,
    )
    candidates = list(db.scalars(stmt).all())
    if customs.sku:
        by_sku = [e for e in candidates if e.sku == customs.sku]
        if len(by_sku) == 1:
            return by_sku[0]
    same_desc = [e for e in candidates if (e.description or "").strip() == (customs.description or "").strip()]
    if len(same_desc) == 1:
        return same_desc[0]
    return None


def _apply_correction(db: Session, replacement: m.CustomsLine, export: m.ExportLine | None) -> None:
    previous = db.get(m.CustomsLine, replacement.revises_id)
    changed = {}
    for field in ("quantity", "amount", "currency", "hs_code", "description"):
        before = getattr(previous, field)
        after = getattr(replacement, field)
        if before != after:
            changed[field] = {"before": jsonable(before), "after": jsonable(after)}
    replacement.changed_fields = changed

    if export is None:
        export = db.scalar(select(m.ExportLine).where(m.ExportLine.customs_line_id == previous.id))
    if export is None:
        return

    previous_amount = export.declared_amount
    previous_qty = export.declared_quantity
    _sync_export_from_customs(db, export, replacement)

    ledger = db.scalar(select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == export.id))
    if ledger is not None and ledger.state in ("submitted", "verified", "rejected", "correction_pending"):
        prev_state = ledger.state
        ledger.state = "correction_pending"
        ledger.correction_open = True
        ledger.version += 1
        db.add(
            m.DeclarationEvent(
                export_line_id=export.id,
                from_state=prev_state,
                to_state="correction_pending",
                actor=db.get(m.SourceDocument, replacement.document_id).ingested_by or "system",
                reason="customs correction affects a submitted goods line",
                source_document_id=replacement.document_id,
                payload=jsonable(
                    {
                        "customs_line_id": replacement.id,
                        "revises_id": replacement.revises_id,
                        "changed_fields": changed,
                        "previous_amount": previous_amount,
                        "previous_quantity": previous_qty,
                    }
                ),
            )
        )

    _mark_confirmed_lines_for_review(
        db,
        export.id,
        reason="customs correction changed the declared basis of a confirmed line",
        source_document_id=replacement.document_id,
    )


def _mark_confirmed_lines_for_review(db: Session, export_line_id: int, reason: str, source_document_id: int | None):
    lines = db.scalars(
        select(m.ReconciliationLine).where(
            m.ReconciliationLine.export_line_id == export_line_id,
            m.ReconciliationLine.status == "active",
        )
    ).all()
    if not lines:
        return
    entity_id = lines[0].entity_id
    db.add(
        m.DifferenceCase(
            entity_id=entity_id,
            kind="reconfirmed_requires_review",
            state="open",
            ref_type="export_line",
            ref_id=export_line_id,
            detail=jsonable(
                {
                    "reason": reason,
                    "reconciliation_line_ids": [l.id for l in lines],
                    "source_document_id": source_document_id,
                    "effect": "occupancy frozen; excluded from confirmed received amount until voided or reassigned",
                }
            ),
        )
    )
    for line in lines:
        line.status = "needs_review"
        line.frozen_at = datetime.now(timezone.utc)
        line.frozen_reason = reason
        bal = db.scalar(
            select(m.Balance).where(m.Balance.side == "export_line", m.Balance.ref_id == export_line_id)
        )
        if bal is not None:
            db.add(
                m.BalanceHold(
                    balance_id=bal.id,
                    side=bal.side,
                    ref_id=bal.ref_id,
                    reconciliation_line_id=line.id,
                    amount=line.amount,
                    action="freeze",
                    actor="system",
                    note=reason,
                )
            )
        db.add(
            m.AuditLog(
                entity_id=entity_id,
                actor="system",
                action="reconciliation.freeze",
                object_type="reconciliation_line",
                object_id=line.id,
                payload=jsonable({"reason": reason, "occupancy": "frozen, not released"}),
            )
        )
    db.flush()


def _sync_export_from_customs(db: Session, export: m.ExportLine, customs: m.CustomsLine) -> None:
    export.customs_line_id = customs.id
    export.declaration_no = customs.declaration_no or export.declaration_no
    export.declared_amount = customs.amount
    export.declared_quantity = customs.quantity
    export.evidence_grade = "A"

    # Freeze confirmed links first: a correction always re-opens confirmed evidence for review.
    _mark_confirmed_lines_for_review(
        db,
        export.id,
        reason="customs correction changed the declared basis of a confirmed line",
        source_document_id=customs.document_id,
    )

    balance = _ensure_balance(db, "export_line", export.id, export.currency, customs.amount)
    # Occupancy is never silently reduced by a correction. The ceiling is max(declared, committed):
    # if the corrected declared amount is below already-frozen occupancy, available stays 0 and the
    # excess is published as a difference case awaiting void/reassignment.
    from .balance import resettle_export_balance

    if balance.committed > customs.amount:
        db.add(
            m.DifferenceCase(
                entity_id=export.entity_id,
                kind="occupancy_exceeds_corrected_amount",
                state="open",
                ref_type="export_line",
                ref_id=export.id,
                detail=jsonable(
                    {
                        "corrected_declared_amount": customs.amount,
                        "committed_amount": balance.committed,
                        "corrected_quantity": customs.quantity,
                        "rule": "confirmed occupancy is not rewritten by a customs correction; "
                        "analyst must void or reassign the frozen lines before the excess can be released",
                    }
                ),
            )
        )
    resettle_export_balance(db, export.id)
    db.flush()


def _ingest_packing(db: Session, entity, doc, req) -> list[int]:
    """Packing list lines corroborate quantity; they never introduce amounts."""
    if entity is None:
        raise HTTPException(422, "packing list requires entity_code")
    if not req.lines:
        raise HTTPException(422, "packing list requires lines")
    ids = []
    for idx, line in enumerate(req.lines, start=1):
        if line.quantity is None:
            raise HTTPException(422, f"packing line {idx}: quantity required")
        obj = m.PackingLine(
            document_id=doc.id,
            entity_id=entity.id,
            line_no=line.line_no or idx,
            sku=line.sku,
            description=line.description,
            quantity=line.quantity,
            packages=line.packages,
            net_weight=line.net_weight,
        )
        db.add(obj)
        db.flush()
        # quantity corroboration against the matching export line (same declaration + sku)
        stmt = select(m.ExportLine).where(
            m.ExportLine.entity_id == entity.id,
            m.ExportLine.declaration_no == line.declaration_no,
        )
        candidates = list(db.scalars(stmt).all())
        if line.sku:
            candidates = [c for c in candidates if c.sku == line.sku]
        if len(candidates) == 1:
            export = candidates[0]
            obj.export_line_id = export.id
            if export.declared_quantity != line.quantity:
                db.add(
                    m.DifferenceCase(
                        entity_id=entity.id,
                        kind="packing_quantity_mismatch",
                        state="open",
                        ref_type="export_line",
                        ref_id=export.id,
                        detail=jsonable(
                            {
                                "packing_line_id": obj.id,
                                "declared_quantity": export.declared_quantity,
                                "packing_quantity": line.quantity,
                                "rule": "packing list corroborates quantity only; it never changes amounts",
                            }
                        ),
                    )
                )
        ids.append(obj.id)
    db.flush()
    return ids


def _ingest_tax(db: Session, entity, doc, req) -> list[int]:
    if entity is None:
        raise HTTPException(422, "tax receipt requires entity_code")
    if not req.lines:
        raise HTTPException(422, "tax receipt requires lines")
    ids = []
    for idx, line in enumerate(req.lines, start=1):
        if line.amount is None or line.currency is None or line.review_status is None:
            raise HTTPException(422, f"tax line {idx}: amount, currency, review_status required")
        issue_time = line.issue_time or req.event_time or req.received_time
        if issue_time is None:
            raise HTTPException(422, f"tax line {idx}: issue_time required (issue time orders receipts)")
        obj = m.TaxLine(
            document_id=doc.id,
            entity_id=entity.id,
            line_no=line.line_no or idx,
            batch_no=line.batch_no or (req.external_no or "batch-unknown"),
            declaration_no=line.declaration_no,
            review_status=line.review_status,
            quantity=line.quantity or Decimal("0"),
            amount=line.amount,
            currency=line.currency,
            issue_time=issue_time,
            sequence_no=line.sequence_no,
        )
        db.add(obj)
        db.flush()
        ids.append(obj.id)
        bind_tax_line(db, obj)
    order_tax_receipts(db, entity.id)
    apply_tax_receipt_states(db, entity.id)
    return ids


def apply_tax_receipt_states(db: Session, entity_id: int) -> dict:
    """Convert declaration states from tax receipts in proven issue order.

    Only receipts whose ordering is proven (no order_conflict) drive transitions, and only lines
    currently awaiting a review verdict (state == submitted) are converted. This keeps late-arriving
    receipts effective at their issue position rather than their arrival position.
    """
    applied: list[dict] = []
    lines = db.scalars(
        select(m.TaxLine)
        .where(
            m.TaxLine.entity_id == entity_id,
            m.TaxLine.export_line_id.is_not(None),
            m.TaxLine.order_conflict.is_(False),
        )
        .order_by(m.TaxLine.issue_time, m.TaxLine.applied_sequence, m.TaxLine.id)
    ).all()
    for tax in lines:
        if tax.review_status not in ("approved", "rejected"):
            continue
        ledger = db.scalar(
            select(m.DeclarationLedger).where(m.DeclarationLedger.export_line_id == tax.export_line_id)
        )
        if ledger is None or ledger.state != "submitted":
            continue
        event = "verified" if tax.review_status == "approved" else "rejected"
        from .ledgers import apply_declaration_event

        apply_declaration_event(
            db,
            ledger.export_line_id and db.get(m.ExportLine, tax.export_line_id),
            event,
            actor=f"tax_receipt:{tax.batch_no}",
            at=tax.issue_time,
            reference=tax.batch_no,
            note=f"applied from tax receipt batch {tax.batch_no} at its issue position",
            source_document_id=tax.document_id,
        )
        applied.append(
            {"tax_line_id": tax.id, "batch_no": tax.batch_no, "export_line_id": tax.export_line_id, "event": event}
        )
    db.flush()
    return {"applied": applied}


def bind_tax_line(db: Session, tax: m.TaxLine) -> None:
    """Bind a tax review line to the export line it reviews (declaration + amount/sku)."""
    stmt = select(m.ExportLine).where(
        m.ExportLine.entity_id == tax.entity_id,
        m.ExportLine.declaration_no == tax.declaration_no,
    )
    candidates = db.scalars(stmt).all()
    if len(candidates) == 1:
        tax.export_line_id = candidates[0].id
        return
    same_amount = [c for c in candidates if c.declared_amount == tax.amount]
    if len(same_amount) == 1:
        tax.export_line_id = same_amount[0].id


def _ordering_proven(a: dict, b: dict) -> bool:
    """Can the relative order of two receipt batches be proven?

    Proven when issue times differ, or when issue times are equal and the verifiable sequence
    numbers differ (a higher sequence is later). Unprovable when issue time ties and either
    sequence is missing, or when equal issue time meets equal sequence numbers.
    """
    if a["issue_time"] != b["issue_time"]:
        return True
    if a["sequence_no"] is None or b["sequence_no"] is None:
        return False
    return a["sequence_no"] != b["sequence_no"]


def order_tax_receipts(db: Session, entity_id: int) -> dict:
    """Order tax receipts by issue time (and verifiable sequence); flag conflicts.

    Ordering is evaluated per receipt batch (tax receipts are returned per declaration batch),
    never per review line:
      * issue time is the primary order; a receipt that arrives late but was issued earlier is
        applied at its issue position (out-of-order arrival is normal, not a conflict).
      * sequence_no is trusted as a tie-breaker only while it does not contradict issue time.
      * when issue time cannot separate two batches and their sequence numbers contradict the
        arrival order, neither batch can prove ordering: every line in them enters a
        receipt-order conflict and no state transition is applied.
    """
    lines = list(
        db.scalars(
            select(m.TaxLine)
            .where(m.TaxLine.entity_id == entity_id)
            .order_by(m.TaxLine.issue_time, m.TaxLine.id)
        ).all()
    )
    batches: dict[str, dict] = {}
    for line in lines:
        b = batches.setdefault(
            line.batch_no,
            {
                "batch_no": line.batch_no,
                "issue_time": line.issue_time,
                "sequence_no": line.sequence_no,
                "received_at": line.created_at,
                "lines": [],
            },
        )
        b["lines"].append(line)
        # a batch is only conflict-free if every line agrees on issue time and sequence
        if line.issue_time != b["issue_time"] or (
            b["sequence_no"] is not None and line.sequence_no != b["sequence_no"]
        ):
            b["inconsistent"] = True

    ordered = sorted(batches.values(), key=lambda b: (b["issue_time"], b["batch_no"]))
    conflicted: set[str] = set()
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if not _ordering_proven(a, b):
                conflicted.update({a["batch_no"], b["batch_no"]})
    for b in ordered:
        if b.get("inconsistent"):
            conflicted.add(b["batch_no"])

    applied_seq = 0
    for b in ordered:
        is_conflict = b["batch_no"] in conflicted
        for line in b["lines"]:
            line.order_conflict = is_conflict
            if is_conflict:
                line.applied_sequence = None
                existing = db.scalar(
                    select(m.DifferenceCase).where(
                        m.DifferenceCase.kind == "receipt_order_conflict",
                        m.DifferenceCase.ref_type == "tax_line",
                        m.DifferenceCase.ref_id == line.id,
                        m.DifferenceCase.state == "open",
                    )
                )
                if existing is None:
                    db.add(
                        m.DifferenceCase(
                            entity_id=entity_id,
                            kind="receipt_order_conflict",
                            state="open",
                            ref_type="tax_line",
                            ref_id=line.id,
                            detail=jsonable(
                                {
                                    "batch_no": line.batch_no,
                                    "issue_time": line.issue_time,
                                    "sequence_no": line.sequence_no,
                                    "received_time": line.created_at,
                                    "rule": "issue time cannot separate receipt batches and the verifiable "
                                    "sequence contradicts arrival order; no state transition applied",
                                }
                            ),
                        )
                    )
                continue
            line.applied_sequence = (
                line.sequence_no if line.sequence_no is not None else applied_seq + 1
            )
        if not is_conflict:
            seq = b["sequence_no"]
            applied_seq = max(applied_seq, seq if seq is not None else applied_seq + 1)
    db.flush()
    _withdraw_states_from_conflicted_receipts(db, entity_id)
    return {"conflicts": sorted(conflicted), "ordered": len(batches)}


def _withdraw_states_from_conflicted_receipts(db: Session, entity_id: int) -> None:
    """If a declaration state was converted by a receipt that has since become order-conflicted,
    the conversion is no longer provable: revert to the pre-conversion state and record why."""
    conflicted_batches = set(
        db.scalars(
            select(m.TaxLine.batch_no).where(
                m.TaxLine.entity_id == entity_id, m.TaxLine.order_conflict.is_(True)
            )
        ).all()
    )
    if not conflicted_batches:
        return
    ledgers = db.scalars(
        select(m.DeclarationLedger)
        .join(m.ExportLine, m.ExportLine.id == m.DeclarationLedger.export_line_id)
        .where(
            m.ExportLine.entity_id == entity_id,
            m.DeclarationLedger.review_ref.in_(conflicted_batches),
            m.DeclarationLedger.state.in_(("verified", "rejected")),
        )
    ).all()
    for ledger in ledgers:
        prev = ledger.state
        ledger.state = "submitted"
        ledger.reviewed_at = None
        ledger.version += 1
        db.add(
            m.DeclarationEvent(
                export_line_id=ledger.export_line_id,
                from_state=prev,
                to_state="submitted",
                actor="system:receipt_order",
                reason=f"receipt {ledger.review_ref} entered order conflict; earlier conversion withdrawn",
                payload=jsonable({"withdrawn_review_ref": ledger.review_ref}),
            )
        )
        ledger.review_ref = None
    db.flush()


def _ingest_bank(db: Session, entity, doc, req) -> list[int]:
    if entity is None:
        raise HTTPException(422, "bank statement requires entity_code")
    if not req.lines:
        raise HTTPException(422, "bank statement requires lines")
    ids = []
    for idx, line in enumerate(req.lines, start=1):
        if line.amount is None or line.currency is None:
            raise HTTPException(422, f"bank line {idx}: amount, currency required")
        ref = line.txn_ref or f"{req.external_no or 'txn'}-{idx}"
        existing = db.scalar(select(m.BankTransaction).where(m.BankTransaction.txn_ref == ref))
        if existing:
            raise HTTPException(409, f"bank transaction already ingested: {ref}")
        unique_ref = _has_unique_reference(line.remittance_info)
        txn = m.BankTransaction(
            document_id=doc.id,
            entity_id=entity.id,
            txn_ref=ref,
            payer_name=line.payer_name,
            currency=line.currency,
            amount=line.amount,
            value_date=line.value_date or req.event_time or req.received_time,
            remittance_info=line.remittance_info,
            has_line_unique_reference=unique_ref,
        )
        db.add(txn)
        db.flush()
        _ensure_balance(db, "bank_transaction", txn.id, txn.currency, txn.amount)
        db.add(m.ReceiptLedger(bank_transaction_id=txn.id, state="unmatched"))
        db.flush()
        ids.append(txn.id)
    return ids


_UNIQUE_REF_PATTERNS = [
    re.compile(r"\bSKU[-_]?[A-Z0-9-]+\b", re.I),  # SKU uniquely identifies one invoice/export line
    re.compile(r"\bINV[-: ]?([A-Z0-9-]+)[-/](\d{1,4})\b", re.I),  # INV-2026-001/3 -> invoice line
    re.compile(r"\bDECL[-: ]?([A-Z0-9-]+)[-/]L(\d{1,4})\b", re.I),  # DECL-X/L2 -> declaration line
]


def _has_unique_reference(remittance_info: str | None) -> bool:
    if not remittance_info:
        return False
    return any(p.search(remittance_info) for p in _UNIQUE_REF_PATTERNS)


def parse_unique_reference(remittance_info: str | None) -> dict | None:
    """Parse remittance info for a unique line-level reference; returns targets or None."""
    if not remittance_info:
        return None
    info = remittance_info
    sku = re.search(r"\b(SKU[-_A-Z0-9]*[A-Z0-9])\b", info, re.I)
    if sku:
        return {"sku": sku.group(1).upper()}
    for decl in re.finditer(r"\bDECL[-: ]?([A-Z0-9-]+)[-/]L(\d{1,4})\b", info, re.I):
        return {"declaration_no": decl.group(1).upper(), "line_no": int(decl.group(2))}
    inv = re.search(r"\bINV[-: ]?([A-Z0-9-]+)[-/](\d{1,4})\b", info, re.I)
    if inv:
        return {"invoice_no": inv.group(1).upper(), "invoice_line_no": int(inv.group(2))}
    return None


def _ensure_balance(db: Session, side: str, ref_id: int, currency: str, total: Decimal) -> m.Balance:
    bal = db.scalar(select(m.Balance).where(m.Balance.side == side, m.Balance.ref_id == ref_id))
    if bal is None:
        bal = m.Balance(
            side=side,
            ref_id=ref_id,
            currency=currency,
            total=total,
            committed=Decimal("0"),
            available=total,
        )
        db.add(bal)
        db.flush()
    return bal


def _ingest_entity_directory(db: Session, doc, req) -> None:
    for idx, line in enumerate(req.lines or [IntakeLine()], start=1):
        code = line.code or (req.external_no if idx == 1 else None)
        if not code:
            raise HTTPException(422, f"entity directory line {idx}: code required")
        existing = db.scalar(select(m.Entity).where(m.Entity.code == code))
        if existing:
            existing.name = line.name or existing.name
            existing.country = line.country or existing.country
        else:
            db.add(m.Entity(code=code, name=line.name or code, country=line.country))


def _ingest_contract(db: Session, entity, doc, req) -> None:
    if entity is None:
        raise HTTPException(422, "contract requires entity_code")
    no = req.external_no or (req.lines[0].contract_no if req.lines else None)
    if not no:
        raise HTTPException(422, "contract requires external_no or contract_no")
    amount = req.amount if req.amount is not None else (req.lines[0].amount if req.lines else None)
    currency = req.currency or (req.lines[0].currency if req.lines else None)
    if amount is None or currency is None:
        raise HTTPException(422, "contract requires amount and currency")
    existing = db.scalar(
        select(m.Contract).where(m.Contract.entity_id == entity.id, m.Contract.contract_no == no)
    )
    if existing:
        existing.amount = amount
        existing.currency = currency.upper()
        existing.source_document_id = doc.id
    else:
        db.add(
            m.Contract(
                entity_id=entity.id,
                contract_no=no,
                buyer_name=req.lines[0].buyer_name if req.lines else None,
                currency=currency.upper(),
                amount=amount,
                effective_date=req.lines[0].effective_date if req.lines else req.event_time,
                source_document_id=doc.id,
            )
        )


def _ingest_return(db: Session, entity, doc, req) -> None:
    """Return document -> reverse candidate. Never proves the export date was ineligible."""
    if entity is None:
        raise HTTPException(422, "return document requires entity_code")
    from .resolver import create_reverse_candidate

    create_reverse_candidate(db, entity, doc, req)
