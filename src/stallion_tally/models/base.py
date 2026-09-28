"""Shared model base."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class TallyRecord(BaseModel):
    """Base for every normalized record. Every record carries its company."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    company_id: str
    company_name: str
    raw_data: dict[str, Any] | None = None
