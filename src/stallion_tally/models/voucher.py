"""Voucher models (Day Book)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stallion_tally.models.base import TallyRecord


class BillAllocation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str | None = None
    bill_type: str | None = None
    amount: Decimal | None = None
    credit_period: str | None = None


class VoucherLedgerEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    line_no: int
    ledger_name: str
    amount: Decimal | None = None
    """Signed amount exactly as Tally reports it (negative = debit)."""
    debit: Decimal | None = None
    credit: Decimal | None = None
    is_deemed_positive: bool | None = None
    is_party_ledger: bool | None = None
    bill_allocations: list[BillAllocation] = Field(default_factory=list)
    extra: dict[str, Any] | None = None


class VoucherInventoryEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    line_no: int
    stock_item_name: str
    quantity: Decimal | None = None
    unit: str | None = None
    actual_quantity: Decimal | None = None
    billed_quantity: Decimal | None = None
    rate: Decimal | None = None
    rate_unit: str | None = None
    amount: Decimal | None = None
    discount: Decimal | None = None
    godown_name: str | None = None
    batch_name: str | None = None
    accounting_ledger: str | None = None
    is_deemed_positive: bool | None = None
    extra: dict[str, Any] | None = None


class Voucher(TallyRecord):
    tally_guid: str = Field(min_length=1)
    voucher_type: str = Field(min_length=1)
    voucher_number: str | None = None
    date: date
    effective_date: date | None = None
    reference: str | None = None
    reference_date: date | None = None
    party_ledger_name: str | None = None
    narration: str | None = None
    alter_id: int | None = None
    master_id: int | None = None
    remote_id: str | None = None
    vch_key: str | None = None
    voucher_key: str | None = None
    persisted_view: str | None = None
    is_cancelled: bool = False
    is_optional: bool = False
    is_invoice: bool | None = None
    is_post_dated: bool | None = None
    total_debit: Decimal | None = None
    total_credit: Decimal | None = None
    ledger_entries: list[VoucherLedgerEntry] = Field(default_factory=list)
    inventory_entries: list[VoucherInventoryEntry] = Field(default_factory=list)
    is_deleted: bool = False
