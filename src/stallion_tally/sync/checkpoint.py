"""Checkpoints: small JSON values that record sync progress per dataset/company."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from stallion_tally.database.models import CheckpointRow, utcnow

VOUCHER_WINDOW_KEY = "voucher_window"


class CheckpointRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, dataset: str, company_id: str, key: str) -> Any:
        row = self.session.execute(
            select(CheckpointRow).where(
                CheckpointRow.dataset == dataset,
                CheckpointRow.company_id == company_id,
                CheckpointRow.key == key,
            )
        ).scalar_one_or_none()
        return None if row is None else row.value_json

    def set(
        self, dataset: str, company_id: str, key: str, value: Any, now: datetime | None = None
    ) -> None:
        row = self.session.execute(
            select(CheckpointRow).where(
                CheckpointRow.dataset == dataset,
                CheckpointRow.company_id == company_id,
                CheckpointRow.key == key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = CheckpointRow(dataset=dataset, company_id=company_id, key=key)
            self.session.add(row)
        row.value_json = value
        row.updated_at = now or utcnow()


@dataclass(frozen=True)
class VoucherWindow:
    from_date: date
    to_date: date
    mode: str  # full | incremental | manual


def plan_voucher_window(
    *,
    today: date,
    now: datetime,
    checkpoint: dict[str, Any] | None,
    lookback_days: int,
    incremental_days: int,
    full_refresh_interval_seconds: int,
    books_from: date | None,
    from_date: date | None = None,
    to_date: date | None = None,
    scheduled: bool = False,
) -> VoucherWindow:
    """Decide which Day Book window to request.

    * explicit dates -> manual window;
    * interactive (`sync` command) -> full lookback window;
    * scheduled agent -> full lookback at most every
      `full_refresh_interval_seconds`, otherwise the shorter incremental window.
    """
    end = to_date or today
    if from_date is not None or to_date is not None:
        start = from_date or (end - timedelta(days=lookback_days))
        mode = "manual"
    else:
        mode = "full"
        if scheduled and checkpoint and checkpoint.get("last_full_refresh_at"):
            last_full = datetime.fromisoformat(checkpoint["last_full_refresh_at"])
            if last_full.tzinfo is None:
                last_full = last_full.replace(tzinfo=UTC)
            if (now - last_full).total_seconds() < full_refresh_interval_seconds:
                mode = "incremental"
        days = lookback_days if mode == "full" else incremental_days
        start = end - timedelta(days=days)
    if books_from is not None and start < books_from:
        start = books_from
    if start > end:
        start = end
    return VoucherWindow(start, end, mode)


def voucher_checkpoint_value(
    previous: dict[str, Any] | None, window: VoucherWindow, run_id: str, now: datetime
) -> dict[str, Any]:
    """Checkpoint value stored after a successful voucher pull."""
    value = dict(previous or {})
    value.update(
        last_window_from=window.from_date.isoformat(),
        last_window_to=window.to_date.isoformat(),
        last_mode=window.mode,
        last_run_id=run_id,
        last_synced_at=now.isoformat(),
    )
    if window.mode == "full":
        value["last_full_refresh_at"] = now.isoformat()
    return value
