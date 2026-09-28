"""Stock item model."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from stallion_tally.models.base import TallyRecord


class StockItem(TallyRecord):
    name: str = Field(min_length=1)
    tally_guid: str | None = None
    parent: str | None = None
    category: str | None = None
    alias: str | None = None
    base_unit: str | None = None
    additional_unit: str | None = None
    alter_id: int | None = None
    master_id: int | None = None
    opening_quantity: Decimal | None = None
    opening_value: Decimal | None = None
    opening_rate: Decimal | None = None
    hsn_code: str | None = None
    part_number: str | None = None
    description: str | None = None
    gst_applicable: str | None = None
    is_deleted: bool = False
