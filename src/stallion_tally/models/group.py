"""Account group model."""

from __future__ import annotations

from pydantic import Field

from stallion_tally.models.base import TallyRecord


class Group(TallyRecord):
    name: str = Field(min_length=1)
    tally_guid: str | None = None
    parent: str | None = None
    alias: str | None = None
    alter_id: int | None = None
    master_id: int | None = None
    is_revenue: bool | None = None
    is_deemed_positive: bool | None = None
    affects_gross_profit: bool | None = None
    is_subledger: bool | None = None
    is_addable: bool | None = None
    sort_position: int | None = None
    is_deleted: bool = False
