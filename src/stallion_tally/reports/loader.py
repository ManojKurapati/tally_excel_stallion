"""Load one company's normalized records from SQLite for a verification report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stallion_tally.database.models import (
    BillRow,
    CompanyRow,
    GroupRow,
    LedgerRow,
    StockItemRow,
    SyncStateRow,
    VoucherInventoryEntryRow,
    VoucherLedgerEntryRow,
    VoucherRow,
)
from stallion_tally.sync.checkpoint import VOUCHER_WINDOW_KEY, CheckpointRepository


@dataclass
class CompanyReportData:
    company: CompanyRow
    period_from: date | None
    period_to: date | None
    period_source: str
    groups: list[GroupRow] = field(default_factory=list)
    ledgers: list[LedgerRow] = field(default_factory=list)
    stock_items: list[StockItemRow] = field(default_factory=list)
    bills: list[BillRow] = field(default_factory=list)
    vouchers: list[VoucherRow] = field(default_factory=list)
    ledger_entries: list[VoucherLedgerEntryRow] = field(default_factory=list)
    inventory_entries: list[VoucherInventoryEntryRow] = field(default_factory=list)
    last_sync: dict[str, datetime | None] = field(default_factory=dict)


def _resolve_period(
    session: Session,
    company_id: str,
    from_date: date | None,
    to_date: date | None,
) -> tuple[date | None, date | None, str]:
    """Pick the voucher period the expert should compare against Tally.

    Explicit dates win. Otherwise use the last Day Book window pulled from
    Tally (the range the local copy is known to be complete for), and finally
    the range of vouchers present locally.
    """
    if from_date is not None or to_date is not None:
        return from_date, to_date, "requested"
    checkpoint: dict[str, Any] | None = CheckpointRepository(session).get(
        "vouchers", company_id, VOUCHER_WINDOW_KEY
    )
    if checkpoint and checkpoint.get("last_window_from") and checkpoint.get("last_window_to"):
        return (
            date.fromisoformat(checkpoint["last_window_from"]),
            date.fromisoformat(checkpoint["last_window_to"]),
            "last Day Book window synced from Tally",
        )
    low, high = session.execute(
        select(func.min(VoucherRow.date), func.max(VoucherRow.date)).where(
            VoucherRow.company_id == company_id, VoucherRow.is_deleted.is_(False)
        )
    ).one()
    return low, high, "all vouchers in the local database"


def _live(model: Any, company_id: str) -> Any:
    return select(model).where(model.company_id == company_id, model.is_deleted.is_(False))


def load_company_report(
    session: Session,
    company: CompanyRow,
    from_date: date | None = None,
    to_date: date | None = None,
) -> CompanyReportData:
    """Everything needed for one company's workbook. Records deleted in Tally are excluded."""
    cid = company.company_id
    period_from, period_to, source = _resolve_period(session, cid, from_date, to_date)
    data = CompanyReportData(company, period_from, period_to, source)

    data.groups = list(session.scalars(_live(GroupRow, cid).order_by(GroupRow.name)))
    data.ledgers = list(session.scalars(_live(LedgerRow, cid).order_by(LedgerRow.name)))
    data.stock_items = list(session.scalars(_live(StockItemRow, cid).order_by(StockItemRow.name)))
    data.bills = list(
        session.scalars(
            _live(BillRow, cid)
            .where(BillRow.status == "outstanding")
            .order_by(BillRow.bill_type, BillRow.ledger_name, BillRow.bill_date, BillRow.bill_ref)
        )
    )

    vouchers = _live(VoucherRow, cid)
    entries = _live(VoucherLedgerEntryRow, cid)
    stock = _live(VoucherInventoryEntryRow, cid)
    if period_from is not None:
        vouchers = vouchers.where(VoucherRow.date >= period_from)
        entries = entries.where(VoucherLedgerEntryRow.voucher_date >= period_from)
        stock = stock.where(VoucherInventoryEntryRow.voucher_date >= period_from)
    if period_to is not None:
        vouchers = vouchers.where(VoucherRow.date <= period_to)
        entries = entries.where(VoucherLedgerEntryRow.voucher_date <= period_to)
        stock = stock.where(VoucherInventoryEntryRow.voucher_date <= period_to)
    data.vouchers = list(
        session.scalars(vouchers.order_by(VoucherRow.date, VoucherRow.master_id, VoucherRow.id))
    )
    data.ledger_entries = list(
        session.scalars(
            entries.order_by(
                VoucherLedgerEntryRow.voucher_date,
                VoucherLedgerEntryRow.voucher_guid,
                VoucherLedgerEntryRow.line_no,
            )
        )
    )
    data.inventory_entries = list(
        session.scalars(
            stock.order_by(
                VoucherInventoryEntryRow.voucher_date,
                VoucherInventoryEntryRow.voucher_guid,
                VoucherInventoryEntryRow.line_no,
            )
        )
    )

    for state in session.scalars(select(SyncStateRow).where(SyncStateRow.company_id == cid)):
        data.last_sync[state.dataset] = state.last_successful_sync
    return data
