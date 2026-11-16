from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOC_TYPES = Literal[
    "invoice",
    "packing_list",
    "customs_archive",
    "customs_correction",
    "tax_receipt",
    "bank_statement",
    "contract",
    "entity_directory",
    "return_document",
    "other",
]

DECLARATION_EVENTS = Literal["submitted", "verified", "rejected", "draft_recorded"]


def _money(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))


def _qty(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.0001"))


class IntakeLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int | None = None
    sku: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None
    # invoice / contract linkage
    invoice_no: str | None = None
    invoice_line_no: int | None = None
    contract_no: str | None = None
    buyer_name: str | None = None
    # customs
    hs_code: str | None = None
    declaration_no: str | None = None
    export_date: datetime | None = None
    revises_line_no: int | None = None
    # tax receipt
    batch_no: str | None = None
    review_status: Literal["approved", "rejected", "pending", "corrected"] | None = None
    issue_time: datetime | None = None
    sequence_no: int | None = None
    # bank statement
    txn_ref: str | None = None
    payer_name: str | None = None
    value_date: datetime | None = None
    remittance_info: str | None = None
    # entity directory / contract
    code: str | None = None
    name: str | None = None
    country: str | None = None
    effective_date: datetime | None = None
    # return document
    transport_shipped_date: datetime | None = None
    transport_returned_date: datetime | None = None
    # packing list
    packages: int | None = None
    net_weight: Decimal | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    _money = field_validator("amount", mode="before")(_money)
    _qty = field_validator("quantity", "net_weight", mode="before")(_qty)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type: DOC_TYPES
    entity_code: str | None = None
    external_no: str | None = None
    original_location: str
    event_time: datetime | None = None
    received_time: datetime | None = None
    currency: str | None = None
    amount: Decimal | None = None
    quantity: Decimal | None = None
    sha256: str | None = None
    source_version: int = 1
    supersedes_document_id: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    lines: list[IntakeLine] = Field(default_factory=list)

    _money = field_validator("amount", mode="before")(_money)
    _qty = field_validator("quantity", mode="before")(_qty)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v):
        return v.upper() if v else v


class DeclarationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: DECLARATION_EVENTS
    at: datetime | None = None
    reference: str | None = None
    note: str | None = None
    source_document_id: int | None = None


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: int | None = None
    bank_transaction_id: int | None = None
    export_line_id: int | None = None
    amount: Decimal | None = None
    note: str | None = None

    _money = field_validator("amount", mode="before")(_money)


class VoidRequest(BaseModel):
    reason: str


class ReassignRequest(BaseModel):
    target_export_line_id: int
    reason: str


class CompileRequest(BaseModel):
    entity_code: str | None = None


class UserCreateRequest(BaseModel):
    name: str
    role: Literal["admin", "analyst", "viewer"]
    api_key: str
    entity_codes: list[str] = Field(default_factory=list)


class EntityCreateRequest(BaseModel):
    code: str
    name: str
    country: str | None = None


# --------------------------------------------------------------------------------------
# Timeliness & status-credibility analysis
# --------------------------------------------------------------------------------------

class RevocationNoticeRequest(BaseModel):
    """税务批准撤销通知：必须说明所指向的批准事件（批次/回执外部号或出口行）。"""

    model_config = ConfigDict(extra="forbid")

    entity_code: str
    external_no: str
    original_location: str
    target_batch_no: str | None = None
    target_document_id: int | None = None
    target_export_line_id: int | None = None
    declaration_no: str | None = None
    issued_at: datetime
    received_at: datetime | None = None
    effective_at: datetime | None = None
    retroactive: bool = False
    sequence_no: int | None = None
    currency: str | None = None
    note: str | None = None
    sha256: str | None = None


class CoverageProofRequest(BaseModel):
    """银行数据覆盖证明 + 数据提取批次：按账户/币种/日期登记 covered/partial/uncovered。"""

    model_config = ConfigDict(extra="forbid")

    entity_code: str
    bank_account: str
    currency: str
    coverage_date: datetime
    status: Literal["covered", "partial", "uncovered"]
    completion: Decimal = Decimal("1.0000")
    extraction_batch_no: str
    extraction_status: Literal["valid", "corrected", "withdrawn"] = "valid"
    issued_at: datetime | None = None
    received_at: datetime | None = None
    sequence_no: int | None = None
    evidence_hash: str | None = None
    note: str | None = None

    _money = field_validator("completion", mode="before")(_money)


class CoverageCorrectionRequest(BaseModel):
    """提取批次更正：把指定账户/币种/日期区间的旧覆盖标记为 superseded 并写入新覆盖。

    只影响后续新分析运行；已引用旧覆盖的历史运行保持原版本不变。
    """

    model_config = ConfigDict(extra="forbid")

    entity_code: str
    bank_account: str
    currency: str
    date_from: datetime
    date_to: datetime
    new_status: Literal["covered", "partial", "uncovered"]
    new_completion: Decimal = Decimal("1.0000")
    extraction_batch_no: str
    corrected_at: datetime | None = None
    note: str | None = None

    _money = field_validator("new_completion", mode="before")(_money)


class TimelinessRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_code: str | None = None
    observation_cutoff: datetime
    # 申报群组过滤：审核批次号 / 币种 / 主体内出口行 id 列表，全部可选
    review_batch_nos: list[str] = Field(default_factory=list)
    currencies: list[str] = Field(default_factory=list)
    export_line_ids: list[int] = Field(default_factory=list)
    mature_horizon_days: int = 30

    @field_validator("currencies")
    @classmethod
    def _upper(cls, v):
        return [c.upper() for c in v if c]
