"""Sync state, run and error bookkeeping."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from stallion_tally.database.models import SyncErrorRow, SyncRunRow, SyncStateRow, utcnow

ALL_COMPANIES = "_all"
"""Pseudo company id used for datasets that are not company specific."""


class SyncStateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- state ----------------------------------------------------------
    def get(self, dataset: str, company_id: str) -> SyncStateRow | None:
        return self.session.execute(
            select(SyncStateRow).where(
                SyncStateRow.dataset == dataset, SyncStateRow.company_id == company_id
            )
        ).scalar_one_or_none()

    def all(self) -> list[SyncStateRow]:
        stmt = select(SyncStateRow).order_by(SyncStateRow.company_name, SyncStateRow.dataset)
        return list(self.session.execute(stmt).scalars())

    def _get_or_create(
        self, dataset: str, company_id: str, company_name: str | None
    ) -> SyncStateRow:
        row = self.get(dataset, company_id)
        if row is None:
            row = SyncStateRow(dataset=dataset, company_id=company_id, company_name=company_name)
            self.session.add(row)
            self.session.flush()
        elif company_name and row.company_name != company_name:
            row.company_name = company_name
        return row

    def mark_started(
        self, dataset: str, company_id: str, company_name: str | None, now: datetime | None = None
    ) -> None:
        now = now or utcnow()
        row = self._get_or_create(dataset, company_id, company_name)
        row.status = "running"
        row.last_started_at = now
        row.updated_at = now

    def mark_pulled(
        self,
        dataset: str,
        company_id: str,
        company_name: str | None,
        records: int,
        now: datetime | None = None,
    ) -> None:
        """Data is in SQLite; the sync is complete only once the export succeeded."""
        now = now or utcnow()
        row = self._get_or_create(dataset, company_id, company_name)
        row.status = "pending_export"
        row.last_successful_sync = now
        row.records_last_run = records
        row.last_error = None
        row.error_count = 0
        row.updated_at = now

    def mark_completed(self, dataset: str, company_id: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        row = self.get(dataset, company_id)
        if row is None:
            return
        row.status = "completed"
        row.last_export = now
        row.last_error = None
        row.updated_at = now

    def mark_failed(
        self,
        dataset: str,
        company_id: str,
        company_name: str | None,
        error: str,
        status: str = "failed",
        now: datetime | None = None,
    ) -> None:
        now = now or utcnow()
        row = self._get_or_create(dataset, company_id, company_name)
        row.status = status
        row.last_error = error[:4000]
        row.error_count = (row.error_count or 0) + 1
        row.updated_at = now

    # --- runs -----------------------------------------------------------
    def start_run(
        self, trigger: str, datasets: list[str], now: datetime | None = None
    ) -> SyncRunRow:
        run = SyncRunRow(
            run_id=str(uuid.uuid4()),
            started_at=now or utcnow(),
            status="running",
            trigger=trigger,
            datasets=",".join(datasets),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self.session.execute(
            update(SyncRunRow)
            .where(SyncRunRow.run_id == run_id)
            .values(status=status, finished_at=now or utcnow(), summary_json=summary, error=error)
        )

    def mark_interrupted_runs(self, now: datetime | None = None) -> int:
        """Runs still marked running at startup were interrupted (crash / restart)."""
        result = self.session.execute(
            update(SyncRunRow)
            .where(SyncRunRow.status == "running")
            .values(status="interrupted", finished_at=now or utcnow())
        )
        self.session.execute(
            update(SyncStateRow)
            .where(SyncStateRow.status == "running")
            .values(status="interrupted")
        )
        return int(result.rowcount or 0)

    def last_run(self) -> SyncRunRow | None:
        return self.session.execute(
            select(SyncRunRow).order_by(SyncRunRow.id.desc()).limit(1)
        ).scalar_one_or_none()

    def last_successful_run(self) -> SyncRunRow | None:
        return self.session.execute(
            select(SyncRunRow)
            .where(SyncRunRow.status == "completed")
            .order_by(SyncRunRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    # --- errors ---------------------------------------------------------
    def record_error(
        self,
        *,
        run_id: str | None,
        company_id: str | None,
        dataset: str | None,
        operation: str,
        error: str,
        retry_count: int = 0,
        now: datetime | None = None,
    ) -> None:
        self.session.add(
            SyncErrorRow(
                timestamp=now or utcnow(),
                run_id=run_id,
                company_id=company_id,
                dataset=dataset,
                operation=operation,
                error=error[:4000],
                retry_count=retry_count,
            )
        )

    def recent_errors(self, limit: int = 20) -> list[SyncErrorRow]:
        stmt = select(SyncErrorRow).order_by(SyncErrorRow.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())
