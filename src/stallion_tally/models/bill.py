"""Outstanding bill model (bills receivable / payable snapshot)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field

from stallion_tally.models.base import TallyRecord

BillType = Literal["receivable", "payable", "unknown"]


class Bill(TallyRecord):
    ledger_name: str = Field(min_length=1)
    bill_ref: str = Field(min_length=1)
    bill_type: BillType = "unknown"
    bill_date: date | None = None
    due_date: date | None = None
    credit_period: str | None = None
    opening_amount: Decimal | None = None
    closing_amount: Decimal | None = None
    overdue_days: int | None = None
    is_advance: bool | None = None
    status: Literal["outstanding", "settled"] = "outstanding"
