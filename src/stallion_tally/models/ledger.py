"""Ledger model."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from stallion_tally.models.base import TallyRecord


class Ledger(TallyRecord):
    name: str = Field(min_length=1)
    tally_guid: str | None = None
    parent_group: str | None = None
    alias: str | None = None
    alter_id: int | None = None
    master_id: int | None = None
    opening_balance: Decimal | None = None
    currency: str | None = None
    is_billwise_on: bool | None = None
    is_cost_centres_on: bool | None = None
    is_revenue: bool | None = None
    is_deemed_positive: bool | None = None
    mailing_name: str | None = None
    address: str | None = None
    state: str | None = None
    country: str | None = None
    pincode: str | None = None
    gstin: str | None = None
    gst_registration_type: str | None = None
    pan: str | None = None
    phone: str | None = None
    mobile: str | None = None
    email: str | None = None
    credit_limit: Decimal | None = None
    credit_period: str | None = None
    is_deleted: bool = False
