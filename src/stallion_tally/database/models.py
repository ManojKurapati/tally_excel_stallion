"""SQLAlchemy ORM tables.

Design rules (see CLAUDE.md):
* every dataset row carries `company_id`;
* uniqueness is enforced by database constraints (idempotent sync);
* rows keep a `record_hash` so unchanged records are not rewritten or
  re-exported, and an `export_status` so failed uploads can be retried.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from stallion_tally.utils.hashing import canonical_json


class MoneyType(TypeDecorator[Decimal]):
    """Exact decimal storage as text (SQLite has no native decimal)."""

    impl = String(48)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return format(Decimal(value), "f")

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None or value == "":
            return None
        return Decimal(value)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone aware UTC timestamps stored as ISO-8601 text."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class JsonText(TypeDecorator[Any]):
    """JSON stored as canonical text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return canonical_json(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        import json

        return json.loads(value)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    pass


class SyncColumns:
    """Columns shared by every dataset table."""

    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    export_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    export_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_seen_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


# Column names that are internal bookkeeping and never exported.
INTERNAL_COLUMNS = frozenset(
    {"id", "export_status", "export_batch_id", "exported_at", "last_seen_run_id"}
)


class AppMeta(Base):
    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class CompanyRow(SyncColumns, Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("company_id", name="uq_companies_company_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    tally_guid: Mapped[str | None] = mapped_column(String(128))
    formal_name: Mapped[str | None] = mapped_column(String(256))
    mailing_name: Mapped[str | None] = mapped_column(String(256))
    company_number: Mapped[str | None] = mapped_column(String(64))
    starting_from: Mapped[date | None] = mapped_column(Date)
    books_from: Mapped[date | None] = mapped_column(Date)
    ending_at: Mapped[date | None] = mapped_column(Date)
    base_currency_symbol: Mapped[str | None] = mapped_column(String(16))
    base_currency_name: Mapped[str | None] = mapped_column(String(64))
    alter_id: Mapped[int | None] = mapped_column(Integer)
    master_id: Mapped[int | None] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    pincode: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class GroupRow(SyncColumns, Base):
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_groups_company_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    tally_guid: Mapped[str | None] = mapped_column(String(128))
    parent: Mapped[str | None] = mapped_column(String(512))
    alias: Mapped[str | None] = mapped_column(String(512))
    alter_id: Mapped[int | None] = mapped_column(Integer)
    master_id: Mapped[int | None] = mapped_column(Integer)
    is_revenue: Mapped[bool | None] = mapped_column(Boolean)
    is_deemed_positive: Mapped[bool | None] = mapped_column(Boolean)
    affects_gross_profit: Mapped[bool | None] = mapped_column(Boolean)
    is_subledger: Mapped[bool | None] = mapped_column(Boolean)
    is_addable: Mapped[bool | None] = mapped_column(Boolean)
    sort_position: Mapped[int | None] = mapped_column(Integer)
    raw_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class LedgerRow(SyncColumns, Base):
    __tablename__ = "ledgers"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_ledgers_company_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    tally_guid: Mapped[str | None] = mapped_column(String(128))
    parent_group: Mapped[str | None] = mapped_column(String(512))
    alias: Mapped[str | None] = mapped_column(String(512))
    alter_id: Mapped[int | None] = mapped_column(Integer)
    master_id: Mapped[int | None] = mapped_column(Integer)
    opening_balance: Mapped[Decimal | None] = mapped_column(MoneyType)
    currency: Mapped[str | None] = mapped_column(String(64))
    is_billwise_on: Mapped[bool | None] = mapped_column(Boolean)
    is_cost_centres_on: Mapped[bool | None] = mapped_column(Boolean)
    is_revenue: Mapped[bool | None] = mapped_column(Boolean)
    is_deemed_positive: Mapped[bool | None] = mapped_column(Boolean)
    mailing_name: Mapped[str | None] = mapped_column(String(512))
    address: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    pincode: Mapped[str | None] = mapped_column(String(32))
    gstin: Mapped[str | None] = mapped_column(String(64))
    gst_registration_type: Mapped[str | None] = mapped_column(String(64))
    pan: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(64))
    mobile: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(256))
    credit_limit: Mapped[Decimal | None] = mapped_column(MoneyType)
    credit_period: Mapped[str | None] = mapped_column(String(64))
    raw_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class StockItemRow(SyncColumns, Base):
    __tablename__ = "stock_items"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_stock_items_company_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    tally_guid: Mapped[str | None] = mapped_column(String(128))
    parent: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(512))
    alias: Mapped[str | None] = mapped_column(String(512))
    base_unit: Mapped[str | None] = mapped_column(String(64))
    additional_unit: Mapped[str | None] = mapped_column(String(64))
    alter_id: Mapped[int | None] = mapped_column(Integer)
    master_id: Mapped[int | None] = mapped_column(Integer)
    opening_quantity: Mapped[Decimal | None] = mapped_column(MoneyType)
    opening_value: Mapped[Decimal | None] = mapped_column(MoneyType)
    opening_rate: Mapped[Decimal | None] = mapped_column(MoneyType)
    hsn_code: Mapped[str | None] = mapped_column(String(32))
    part_number: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    gst_applicable: Mapped[str | None] = mapped_column(String(64))
    raw_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class BillRow(SyncColumns, Base):
    __tablename__ = "bills"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "ledger_name", "bill_ref", name="uq_bills_company_ledger_ref"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    ledger_name: Mapped[str] = mapped_column(String(512), nullable=False)
    bill_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    bill_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    bill_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    credit_period: Mapped[str | None] = mapped_column(String(64))
    opening_amount: Mapped[Decimal | None] = mapped_column(MoneyType)
    closing_amount: Mapped[Decimal | None] = mapped_column(MoneyType)
    overdue_days: Mapped[int | None] = mapped_column(Integer)
    is_advance: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="outstanding")
    snapshot_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    raw_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class VoucherRow(SyncColumns, Base):
    __tablename__ = "vouchers"
    __table_args__ = (
        UniqueConstraint("company_id", "tally_guid", name="uq_vouchers_company_guid"),
        Index("ix_vouchers_company_date", "company_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    tally_guid: Mapped[str] = mapped_column(String(128), nullable=False)
    voucher_type: Mapped[str] = mapped_column(String(256), nullable=False)
    voucher_number: Mapped[str | None] = mapped_column(String(256))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(String(512))
    reference_date: Mapped[date | None] = mapped_column(Date)
    party_ledger_name: Mapped[str | None] = mapped_column(String(512))
    narration: Mapped[str | None] = mapped_column(Text)
    alter_id: Mapped[int | None] = mapped_column(Integer)
    master_id: Mapped[int | None] = mapped_column(Integer)
    remote_id: Mapped[str | None] = mapped_column(String(128))
    vch_key: Mapped[str | None] = mapped_column(String(128))
    voucher_key: Mapped[str | None] = mapped_column(String(128))
    persisted_view: Mapped[str | None] = mapped_column(String(64))
    is_cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_invoice: Mapped[bool | None] = mapped_column(Boolean)
    is_post_dated: Mapped[bool | None] = mapped_column(Boolean)
    total_debit: Mapped[Decimal | None] = mapped_column(MoneyType)
    total_credit: Mapped[Decimal | None] = mapped_column(MoneyType)
    ledger_entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inventory_entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class VoucherLedgerEntryRow(SyncColumns, Base):
    __tablename__ = "voucher_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "voucher_guid", "line_no", name="uq_voucher_ledger_entries_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    voucher_guid: Mapped[str] = mapped_column(String(128), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    voucher_date: Mapped[date | None] = mapped_column(Date)
    voucher_type: Mapped[str | None] = mapped_column(String(256))
    voucher_number: Mapped[str | None] = mapped_column(String(256))
    ledger_name: Mapped[str] = mapped_column(String(512), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(MoneyType)
    debit: Mapped[Decimal | None] = mapped_column(MoneyType)
    credit: Mapped[Decimal | None] = mapped_column(MoneyType)
    is_deemed_positive: Mapped[bool | None] = mapped_column(Boolean)
    is_party_ledger: Mapped[bool | None] = mapped_column(Boolean)
    bill_allocations_json: Mapped[Any] = mapped_column(JsonText, nullable=True)
    extra_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


class VoucherInventoryEntryRow(SyncColumns, Base):
    __tablename__ = "voucher_inventory_entries"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "voucher_guid", "line_no", name="uq_voucher_inventory_entries_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    voucher_guid: Mapped[str] = mapped_column(String(128), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    voucher_date: Mapped[date | None] = mapped_column(Date)
    voucher_type: Mapped[str | None] = mapped_column(String(256))
    voucher_number: Mapped[str | None] = mapped_column(String(256))
    stock_item_name: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(MoneyType)
    unit: Mapped[str | None] = mapped_column(String(64))
    actual_quantity: Mapped[Decimal | None] = mapped_column(MoneyType)
    billed_quantity: Mapped[Decimal | None] = mapped_column(MoneyType)
    rate: Mapped[Decimal | None] = mapped_column(MoneyType)
    rate_unit: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal | None] = mapped_column(MoneyType)
    discount: Mapped[Decimal | None] = mapped_column(MoneyType)
    godown_name: Mapped[str | None] = mapped_column(String(512))
    batch_name: Mapped[str | None] = mapped_column(String(512))
    accounting_ledger: Mapped[str | None] = mapped_column(String(512))
    is_deemed_positive: Mapped[bool | None] = mapped_column(Boolean)
    extra_json: Mapped[Any] = mapped_column(JsonText, nullable=True)


# ---------------------------------------------------------------------------
# Sync bookkeeping
# ---------------------------------------------------------------------------


class SyncStateRow(Base):
    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("dataset", "company_id", name="uq_sync_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="never")
    last_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_successful_sync: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_export: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_last_run: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("dataset", "company_id", "key", name="uq_checkpoints"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_json: Mapped[Any] = mapped_column(JsonText, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class SyncRunRow(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="cli")
    datasets: Mapped[str | None] = mapped_column(String(256))
    summary_json: Mapped[Any] = mapped_column(JsonText, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)


class SyncErrorRow(Base):
    __tablename__ = "sync_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    company_id: Mapped[str | None] = mapped_column(String(128))
    dataset: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExportBatchRow(Base):
    __tablename__ = "export_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    company_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    export_date: Mapped[str] = mapped_column(String(10), nullable=False)
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_name: Mapped[str] = mapped_column(String(256), nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False)
    blob_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_blob_path: Mapped[str | None] = mapped_column(Text)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    content_md5: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class RawFileRow(Base):
    __tablename__ = "raw_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(36))
    company_id: Mapped[str | None] = mapped_column(String(128))
    company_name: Mapped[str | None] = mapped_column(String(256))
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    blob_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    uploaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


DATASET_TABLES: dict[str, type[Base]] = {
    "companies": CompanyRow,
    "groups": GroupRow,
    "ledgers": LedgerRow,
    "stock_items": StockItemRow,
    "bills": BillRow,
    "vouchers": VoucherRow,
    "voucher_ledger_entries": VoucherLedgerEntryRow,
    "voucher_inventory_entries": VoucherInventoryEntryRow,
}

NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "companies": ("company_id",),
    "groups": ("company_id", "name"),
    "ledgers": ("company_id", "name"),
    "stock_items": ("company_id", "name"),
    "bills": ("company_id", "ledger_name", "bill_ref"),
    "vouchers": ("company_id", "tally_guid"),
    "voucher_ledger_entries": ("company_id", "voucher_guid", "line_no"),
    "voucher_inventory_entries": ("company_id", "voucher_guid", "line_no"),
}
