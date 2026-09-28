"""Repositories: idempotent upserts, deletion reconciliation and export bookkeeping.

Upserts compare record hashes so unchanged rows are neither rewritten nor
re-exported. Uniqueness is guaranteed by the table constraints and the
SQLite `INSERT ... ON CONFLICT DO UPDATE` statement, not by application
level checks alone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from stallion_tally.database.models import (
    DATASET_TABLES,
    NATURAL_KEYS,
    Base,
    BillRow,
    CompanyRow,
    ExportBatchRow,
    RawFileRow,
    VoucherInventoryEntryRow,
    VoucherLedgerEntryRow,
    VoucherRow,
    utcnow,
)

UPSERT_CHUNK = 100
IN_CHUNK = 500
_NOT_UPDATED = {"id", "created_at", "export_batch_id", "exported_at"}


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def merge(self, other: UpsertResult) -> UpsertResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        self.rejected += other.rejected
        self.errors.extend(other.errors)
        return self


class DatasetRepository:
    """Generic hash-compared upsert for one dataset table."""

    def __init__(self, session: Session, dataset: str) -> None:
        self.session = session
        self.dataset = dataset
        self.model: type[Base] = DATASET_TABLES[dataset]
        self.table = self.model.__table__  # type: ignore[attr-defined]
        self.keys = NATURAL_KEYS[dataset]

    # ------------------------------------------------------------------ helpers
    def _key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[k] for k in self.keys)

    def _existing(
        self, rows: Sequence[dict[str, Any]]
    ) -> dict[tuple[Any, ...], tuple[int, str, bool, Any]]:
        """Map natural key -> (id, record_hash, is_deleted, status) for keys present in `rows`."""
        result: dict[tuple[Any, ...], tuple[int, str, bool, Any]] = {}
        if not rows:
            return result
        key_cols = [self.table.c[k] for k in self.keys]
        wanted = {self._key(r) for r in rows}
        status_col = self.table.c.status if "status" in self.table.c else None
        prefix_col = key_cols[min(1, len(key_cols) - 1)]
        prefix_values = sorted({r[prefix_col.name] for r in rows}, key=str)
        company_ids = {r["company_id"] for r in rows}
        for values in chunked(prefix_values, IN_CHUNK):
            stmt = select(
                self.table.c.id,
                self.table.c.record_hash,
                self.table.c.is_deleted,
                status_col if status_col is not None else self.table.c.id,
                *key_cols,
            )
            if len(key_cols) > 1:
                stmt = stmt.where(self.table.c.company_id.in_(company_ids))
            stmt = stmt.where(prefix_col.in_(values))
            for row in self.session.execute(stmt):
                key = tuple(row[4:])
                if key in wanted:
                    result[key] = (
                        row[0],
                        row[1],
                        bool(row[2]),
                        row[3] if status_col is not None else None,
                    )
        return result

    # ------------------------------------------------------------------ upsert
    def upsert(
        self, rows: Sequence[dict[str, Any]], run_id: str, now: datetime | None = None
    ) -> UpsertResult:
        now = now or utcnow()
        result = UpsertResult()
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            deduped[self._key(row)] = row  # last occurrence wins
        unique_rows = list(deduped.values())
        existing = self._existing(unique_rows)

        to_write: list[dict[str, Any]] = []
        unchanged_ids: list[int] = []
        for row in unique_rows:
            current = existing.get(self._key(row))
            if current is None:
                result.inserted += 1
            elif (
                current[1] != row["record_hash"]
                or current[2] != bool(row.get("is_deleted", False))
                or (current[3] is not None and "status" in row and current[3] != row["status"])
            ):
                result.updated += 1
            else:
                result.unchanged += 1
                unchanged_ids.append(current[0])
                continue
            payload = dict(row)
            payload.setdefault("is_deleted", False)
            payload.update(
                export_status="pending",
                last_seen_run_id=run_id,
                updated_at=now,
                created_at=now,
                export_batch_id=None,
                exported_at=None,
            )
            to_write.append(payload)

        column_names = {c.name for c in self.table.columns}
        for chunk in chunked(to_write, UPSERT_CHUNK):
            values = [{k: v for k, v in row.items() if k in column_names} for row in chunk]
            stmt = sqlite_insert(self.table).values(values)
            set_ = {
                c.name: getattr(stmt.excluded, c.name)
                for c in self.table.columns
                if c.name not in _NOT_UPDATED and c.name not in self.keys
            }
            stmt = stmt.on_conflict_do_update(index_elements=list(self.keys), set_=set_)
            self.session.execute(stmt)

        for ids in chunked(unchanged_ids, IN_CHUNK):
            self.session.execute(
                update(self.table).where(self.table.c.id.in_(ids)).values(last_seen_run_id=run_id)
            )
        return result

    def touch(self, keys: Sequence[tuple[Any, ...]], run_id: str) -> int:
        """Mark existing rows with these natural keys as seen in `run_id` (no content change)."""
        if not keys:
            return 0
        rows = [dict(zip(self.keys, key, strict=True)) for key in keys]
        ids = [entry[0] for entry in self._existing(rows).values()]
        for chunk in chunked(ids, IN_CHUNK):
            self.session.execute(
                update(self.table).where(self.table.c.id.in_(chunk)).values(last_seen_run_id=run_id)
            )
        return len(ids)

    # ------------------------------------------------------------------ reconciliation
    def mark_missing(
        self,
        company_id: str,
        run_id: str,
        *,
        values: dict[str, Any],
        date_from: date | None = None,
        date_to: date | None = None,
        now: datetime | None = None,
    ) -> list[tuple[Any, ...]]:
        """Apply `values` to rows of the company not seen in `run_id`.

        Returns the natural keys of the affected rows.
        """
        now = now or utcnow()
        conditions = [
            self.table.c.company_id == company_id,
            self.table.c.is_deleted.is_(False),
            or_(self.table.c.last_seen_run_id.is_(None), self.table.c.last_seen_run_id != run_id),
        ]
        if "status" in self.table.c and "status" in values:
            conditions.append(self.table.c.status != values["status"])
        if date_from is not None and "date" in self.table.c:
            conditions.append(self.table.c.date >= date_from)
        if date_to is not None and "date" in self.table.c:
            conditions.append(self.table.c.date <= date_to)
        key_cols = [self.table.c[k] for k in self.keys]
        affected = [tuple(r) for r in self.session.execute(select(*key_cols).where(*conditions))]
        if affected:
            self.session.execute(
                update(self.table)
                .where(*conditions)
                .values(export_status="pending", updated_at=now, **values)
            )
        return affected

    def mark_missing_deleted(
        self, company_id: str, run_id: str, **kwargs: Any
    ) -> list[tuple[Any, ...]]:
        return self.mark_missing(company_id, run_id, values={"is_deleted": True}, **kwargs)

    # ------------------------------------------------------------------ queries
    def count(self, company_id: str | None = None, include_deleted: bool = False) -> int:
        stmt = select(func.count()).select_from(self.table)
        if company_id is not None:
            stmt = stmt.where(self.table.c.company_id == company_id)
        if not include_deleted:
            stmt = stmt.where(self.table.c.is_deleted.is_(False))
        return int(self.session.execute(stmt).scalar_one())


class VoucherRepository:
    """Vouchers and their child entry tables."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.vouchers = DatasetRepository(session, "vouchers")
        self.ledger_entries = DatasetRepository(session, "voucher_ledger_entries")
        self.inventory_entries = DatasetRepository(session, "voucher_inventory_entries")

    def upsert(
        self,
        vouchers: Sequence[dict[str, Any]],
        ledger_entries: Sequence[dict[str, Any]],
        inventory_entries: Sequence[dict[str, Any]],
        run_id: str,
        now: datetime | None = None,
    ) -> UpsertResult:
        now = now or utcnow()
        result = self.vouchers.upsert(vouchers, run_id, now)
        self.ledger_entries.upsert(ledger_entries, run_id, now)
        self.inventory_entries.upsert(inventory_entries, run_id, now)
        # Lines that disappeared from a voucher become tombstones.
        for voucher in vouchers:
            self._trim_entries(
                VoucherLedgerEntryRow,
                voucher["company_id"],
                voucher["tally_guid"],
                voucher.get("ledger_entry_count", 0),
                now,
            )
            self._trim_entries(
                VoucherInventoryEntryRow,
                voucher["company_id"],
                voucher["tally_guid"],
                voucher.get("inventory_entry_count", 0),
                now,
            )
        return result

    def _trim_entries(
        self, model: type[Base], company_id: str, guid: str, keep: int, now: datetime
    ) -> None:
        table = model.__table__  # type: ignore[attr-defined]
        self.session.execute(
            update(table)
            .where(
                table.c.company_id == company_id,
                table.c.voucher_guid == guid,
                table.c.line_no > keep,
                table.c.is_deleted.is_(False),
            )
            .values(is_deleted=True, export_status="pending", updated_at=now)
        )

    def mark_missing_deleted(
        self,
        company_id: str,
        run_id: str,
        date_from: date,
        date_to: date,
        now: datetime | None = None,
    ) -> int:
        """Soft delete vouchers in the window that Tally no longer returned."""
        now = now or utcnow()
        deleted = self.vouchers.mark_missing_deleted(
            company_id, run_id, date_from=date_from, date_to=date_to, now=now
        )
        guids = [key[1] for key in deleted]
        for model in (VoucherLedgerEntryRow, VoucherInventoryEntryRow):
            table = model.__table__  # type: ignore[attr-defined]
            for chunk in chunked(guids, IN_CHUNK):
                self.session.execute(
                    update(table)
                    .where(
                        table.c.company_id == company_id,
                        table.c.voucher_guid.in_(chunk),
                        table.c.is_deleted.is_(False),
                    )
                    .values(is_deleted=True, export_status="pending", updated_at=now)
                )
        return len(deleted)


class CompanyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def all(self) -> list[CompanyRow]:
        return list(
            self.session.execute(select(CompanyRow).order_by(CompanyRow.company_name)).scalars()
        )

    def get(self, company_id: str) -> CompanyRow | None:
        return self.session.execute(
            select(CompanyRow).where(CompanyRow.company_id == company_id)
        ).scalar_one_or_none()


class BillRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.bills = DatasetRepository(session, "bills")

    def mark_missing_settled(
        self, company_id: str, run_id: str, now: datetime | None = None
    ) -> int:
        affected = self.bills.mark_missing(
            company_id, run_id, values={"status": "settled"}, now=now
        )
        return len(affected)


# ---------------------------------------------------------------------------
# Export bookkeeping
# ---------------------------------------------------------------------------

INCOMPLETE_BATCH_STATUSES = ("created", "uploaded", "failed")


class ExportRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _table(dataset: str) -> Any:
        return DATASET_TABLES[dataset].__table__  # type: ignore[attr-defined]

    def pending_count(self, dataset: str, company_id: str | None = None) -> int:
        table = self._table(dataset)
        stmt = select(func.count()).select_from(table).where(table.c.export_status == "pending")
        if company_id is not None:
            stmt = stmt.where(table.c.company_id == company_id)
        return int(self.session.execute(stmt).scalar_one())

    def pending_companies(self, dataset: str) -> list[tuple[str, str]]:
        table = self._table(dataset)
        stmt = (
            select(table.c.company_id, table.c.company_name)
            .where(table.c.export_status == "pending", table.c.export_batch_id.is_(None))
            .distinct()
            .order_by(table.c.company_name)
        )
        return [(r[0], r[1]) for r in self.session.execute(stmt)]

    def fetch_pending(self, dataset: str, company_id: str, limit: int) -> list[Any]:
        model = DATASET_TABLES[dataset]
        table = self._table(dataset)
        stmt = (
            select(model)
            .where(
                table.c.company_id == company_id,
                table.c.export_status == "pending",
                table.c.export_batch_id.is_(None),
            )
            .order_by(table.c.id)
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def assign_batch(self, dataset: str, ids: Sequence[int], batch_id: str) -> None:
        table = self._table(dataset)
        for chunk in chunked(list(ids), IN_CHUNK):
            self.session.execute(
                update(table).where(table.c.id.in_(chunk)).values(export_batch_id=batch_id)
            )

    def mark_batch_exported(self, batch: ExportBatchRow, now: datetime | None = None) -> int:
        """Mark rows of a verified batch exported.

        Rows modified after the batch file was written stay pending and are
        released so the next batch picks them up with their newer content.
        """
        now = now or utcnow()
        table = self._table(batch.dataset)
        result = self.session.execute(
            update(table)
            .where(
                table.c.export_batch_id == batch.batch_id,
                table.c.export_status == "pending",
                table.c.updated_at <= batch.created_at,
            )
            .values(export_status="exported", exported_at=now, export_batch_id=None)
        )
        # Rows changed after the file was written (still pending) are released for the next batch.
        self.session.execute(
            update(table)
            .where(table.c.export_batch_id == batch.batch_id)
            .values(export_batch_id=None)
        )
        return int(result.rowcount or 0)

    def release_batch(self, batch: ExportBatchRow) -> None:
        table = self._table(batch.dataset)
        self.session.execute(
            update(table)
            .where(table.c.export_batch_id == batch.batch_id, table.c.export_status == "pending")
            .values(export_batch_id=None)
        )

    def next_part_number(self, company_id: str, dataset: str, export_date: str) -> int:
        stmt = select(func.max(ExportBatchRow.part_number)).where(
            ExportBatchRow.company_id == company_id,
            ExportBatchRow.dataset == dataset,
            ExportBatchRow.export_date == export_date,
        )
        current = self.session.execute(stmt).scalar_one()
        return int(current or 0) + 1

    def create_batch(self, **fields: Any) -> ExportBatchRow:
        batch = ExportBatchRow(batch_id=str(uuid.uuid4()), **fields)
        self.session.add(batch)
        self.session.flush()
        return batch

    def get_batch(self, batch_id: str) -> ExportBatchRow | None:
        return self.session.execute(
            select(ExportBatchRow).where(ExportBatchRow.batch_id == batch_id)
        ).scalar_one_or_none()

    def incomplete_batches(
        self, company_ids: Iterable[str] | None = None, datasets: Iterable[str] | None = None
    ) -> list[ExportBatchRow]:
        stmt = select(ExportBatchRow).where(ExportBatchRow.status.in_(INCOMPLETE_BATCH_STATUSES))
        if company_ids is not None:
            stmt = stmt.where(ExportBatchRow.company_id.in_(list(company_ids)))
        if datasets is not None:
            stmt = stmt.where(ExportBatchRow.dataset.in_(list(datasets)))
        return list(self.session.execute(stmt.order_by(ExportBatchRow.id)).scalars())

    def batches(self, limit: int = 50) -> list[ExportBatchRow]:
        stmt = select(ExportBatchRow).order_by(ExportBatchRow.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())

    def batch_counts_by_status(self) -> dict[str, int]:
        stmt = select(ExportBatchRow.status, func.count()).group_by(ExportBatchRow.status)
        return {status: int(count) for status, count in self.session.execute(stmt)}

    # --- raw files -------------------------------------------------------
    def add_raw_file(self, **fields: Any) -> RawFileRow:
        existing = self.session.execute(
            select(RawFileRow).where(RawFileRow.local_path == fields["local_path"])
        ).scalar_one_or_none()
        if existing is not None:
            for key, value in fields.items():
                setattr(existing, key, value)
            return existing
        row = RawFileRow(**fields)
        self.session.add(row)
        self.session.flush()
        return row

    def pending_raw_files(self, company_ids: Iterable[str] | None = None) -> list[RawFileRow]:
        stmt = select(RawFileRow).where(RawFileRow.uploaded.is_(False))
        if company_ids is not None:
            ids = list(company_ids)
            stmt = stmt.where(or_(RawFileRow.company_id.in_(ids), RawFileRow.company_id.is_(None)))
        return list(self.session.execute(stmt.order_by(RawFileRow.id)).scalars())

    def uploaded_raw_files_before(self, cutoff: datetime) -> list[RawFileRow]:
        stmt = select(RawFileRow).where(
            RawFileRow.uploaded.is_(True), RawFileRow.created_at <= cutoff
        )
        return list(self.session.execute(stmt).scalars())

    def delete_raw_file(self, row: RawFileRow) -> None:
        self.session.delete(row)


__all__ = [
    "BillRepository",
    "BillRow",
    "CompanyRepository",
    "DatasetRepository",
    "ExportRepository",
    "UpsertResult",
    "VoucherRepository",
    "VoucherRow",
    "chunked",
]
