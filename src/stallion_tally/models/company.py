"""Company model."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stallion_tally.utils.text import slugify


class Company(BaseModel):
    """A company loaded in TallyPrime.

    `company_id` is the Tally company GUID when available, otherwise a slug of
    the company name. It is the stable identifier stamped on every record.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    company_id: str = ""
    name: str = Field(min_length=1)
    tally_guid: str | None = None
    formal_name: str | None = None
    mailing_name: str | None = None
    company_number: str | None = None
    starting_from: date | None = None
    books_from: date | None = None
    ending_at: date | None = None
    base_currency_symbol: str | None = None
    base_currency_name: str | None = None
    alter_id: int | None = None
    master_id: int | None = None
    email: str | None = None
    state: str | None = None
    country: str | None = None
    pincode: str | None = None
    address: str | None = None
    raw_data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _derive_company_id(self) -> Company:
        if not self.name:
            raise ValueError("company name is required")
        if not self.company_id:
            self.company_id = self.tally_guid or slugify(self.name)
        return self
