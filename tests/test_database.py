from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from stallion_tally.database import Database
from stallion_tally.database.models import LedgerRow, VoucherLedgerEntryRow, VoucherRow
from stallion_tally.database.repositories import (
    BillRepository,
    DatasetRepository,
    ExportRepository,
    VoucherRepository,
)
from stallion_tally.models import Bill, Ledger, Voucher, VoucherLedgerEntry
from stallion_tally.sync.normalize import normalize_bill, normalize_ledger, normalize_voucher
from stallion_tally.sync.state import SyncStateRepository
from tests.conftest import NOW


def ledger(name: str, parent: str = "Sundry Debtors") -> dict:
    return normalize_ledger(
        Ledger(company_id="c1", company_name="Co", name=name, parent_group=parent)
    )


def voucher(guid: str, lines: int = 2, narration: str = "x") -> Voucher:
    return Voucher(
        company_id="c1",
        company_name="Co",
        tally_guid=guid,
        voucher_type="Journal",
        date=date(2026, 9, 10),
        narration=narration,
        ledger_entries=[
            VoucherLedgerEntry(line_no=i + 1, ledger_name=f"L{i}", amount=Decimal(i))
            for i in range(lines)
        ],
    )


def count(db: Database, model: type) -> int:
    with db.session() as s:
        return s.execute(select(func.count()).select_from(model)).scalar_one()


def test_upsert_is_idempotent(database: Database) -> None:
    rows = [ledger("A"), ledger("B")]
    with database.session() as s:
        first = DatasetRepository(s, "ledgers").upsert(rows, "run-1", NOW)
    with database.session() as s:
        second = DatasetRepository(s, "ledgers").upsert(rows, "run-2", NOW)
    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert count(database, LedgerRow) == 2
    with database.session() as s:
        row = s.execute(select(LedgerRow).where(LedgerRow.name == "A")).scalar_one()
        assert row.last_seen_run_id == "run-2" and row.export_status == "pending"


def test_changed_record_is_updated_and_marked_pending(database: Database) -> None:
    with database.session() as s:
        repo = DatasetRepository(s, "ledgers")
        repo.upsert([ledger("A")], "run-1", NOW)
        ExportRepository(s).assign_batch("ledgers", [1], "batch-1")
        s.execute(LedgerRow.__table__.update().values(export_status="exported"))
    with database.session() as s:
        result = DatasetRepository(s, "ledgers").upsert(
            [ledger("A", parent="Bank Accounts")], "run-2", NOW
        )
        assert result.updated == 1
        row = s.execute(select(LedgerRow)).scalar_one()
        assert row.parent_group == "Bank Accounts" and row.export_status == "pending"
        assert row.export_batch_id == "batch-1"  # batch assignment preserved for in-flight batches


def test_duplicate_within_batch_is_deduplicated(database: Database) -> None:
    with database.session() as s:
        result = DatasetRepository(s, "ledgers").upsert(
            [ledger("A"), ledger("A", parent="Other")], "run-1", NOW
        )
    assert result.inserted == 1
    assert count(database, LedgerRow) == 1
    with database.session() as s:
        assert s.execute(select(LedgerRow.parent_group)).scalar_one() == "Other"


def test_unique_constraint_enforced_by_database(database: Database) -> None:
    with database.session() as s:
        DatasetRepository(s, "ledgers").upsert([ledger("A")], "run-1", NOW)
    with pytest.raises(IntegrityError):
        with database.session() as s:
            s.add(LedgerRow(company_id="c1", company_name="Co", name="A", record_hash="x"))
            s.flush()


def test_mark_missing_deleted_only_touches_unseen_rows(database: Database) -> None:
    with database.session() as s:
        DatasetRepository(s, "ledgers").upsert([ledger("A"), ledger("B")], "run-1", NOW)
    with database.session() as s:
        repo = DatasetRepository(s, "ledgers")
        repo.upsert([ledger("A")], "run-2", NOW)
        deleted = repo.mark_missing_deleted("c1", "run-2", now=NOW)
        assert deleted == [("c1", "B")]
        rows = {r.name: r for r in s.execute(select(LedgerRow)).scalars()}
        assert rows["B"].is_deleted is True and rows["A"].is_deleted is False
    # A deleted record that reappears is resurrected and marked pending again.
    with database.session() as s:
        result = DatasetRepository(s, "ledgers").upsert([ledger("A"), ledger("B")], "run-3", NOW)
        assert result.updated == 1
        assert all(not r.is_deleted for r in s.execute(select(LedgerRow)).scalars())


def test_voucher_entries_trimmed_and_deleted(database: Database) -> None:
    def rows(v: Voucher) -> tuple:
        r, le, ie = normalize_voucher(v)
        return [r], le, ie

    with database.session() as s:
        VoucherRepository(s).upsert(*rows(voucher("g1", lines=3)), run_id="run-1", now=NOW)
    assert count(database, VoucherLedgerEntryRow) == 3
    with database.session() as s:
        result = VoucherRepository(s).upsert(
            *rows(voucher("g1", lines=2, narration="changed")), run_id="run-2", now=NOW
        )
        assert result.updated == 1
        entries = {e.line_no: e for e in s.execute(select(VoucherLedgerEntryRow)).scalars()}
        assert entries[3].is_deleted is True and entries[3].export_status == "pending"
        assert entries[1].is_deleted is False
    with database.session() as s:
        deleted = VoucherRepository(s).mark_missing_deleted(
            "c1", "run-3", date(2026, 9, 1), date(2026, 9, 30), NOW
        )
        assert deleted == 1
        assert s.execute(select(VoucherRow)).scalar_one().is_deleted is True
        assert all(e.is_deleted for e in s.execute(select(VoucherLedgerEntryRow)).scalars())


def test_voucher_outside_window_not_deleted(database: Database) -> None:
    r, le, ie = normalize_voucher(voucher("g1"))
    with database.session() as s:
        VoucherRepository(s).upsert([r], le, ie, "run-1", NOW)
    with database.session() as s:
        assert (
            VoucherRepository(s).mark_missing_deleted(
                "c1", "run-2", date(2026, 10, 1), date(2026, 10, 31), NOW
            )
            == 0
        )


def test_bills_settled_when_missing(database: Database) -> None:
    bill = Bill(
        company_id="c1",
        company_name="Co",
        ledger_name="L",
        bill_ref="B1",
        closing_amount=Decimal("5"),
    )
    with database.session() as s:
        BillRepository(s).bills.upsert([normalize_bill(bill, NOW)], "run-1", NOW)
    with database.session() as s:
        assert BillRepository(s).mark_missing_settled("c1", "run-2", NOW) == 1
    with database.session() as s:
        from stallion_tally.database.models import BillRow

        row = s.execute(select(BillRow)).scalar_one()
        assert row.status == "settled" and row.export_status == "pending"
    with database.session() as s:  # bill reappears -> outstanding again
        assert (
            BillRepository(s).bills.upsert([normalize_bill(bill, NOW)], "run-3", NOW).updated == 1
        )


def test_money_type_roundtrip_exact(database: Database) -> None:
    with database.session() as s:
        DatasetRepository(s, "ledgers").upsert(
            [
                normalize_ledger(
                    Ledger(
                        company_id="c1",
                        company_name="Co",
                        name="A",
                        opening_balance=Decimal("123456789012.123456"),
                    )
                )
            ],
            "run-1",
            NOW,
        )
    with database.session() as s:
        assert s.execute(select(LedgerRow.opening_balance)).scalar_one() == Decimal(
            "123456789012.123456"
        )


def test_sync_state_and_runs(database: Database) -> None:
    with database.session() as s:
        state = SyncStateRepository(s)
        run = state.start_run("cli", ["ledgers"], NOW)
        state.mark_started("ledgers", "c1", "Co", NOW)
        state.mark_pulled("ledgers", "c1", "Co", 10, NOW)
        state.record_error(
            run_id=run.run_id,
            company_id="c1",
            dataset="ledgers",
            operation="x",
            error="boom",
            now=NOW,
        )
    with database.session() as s:
        state = SyncStateRepository(s)
        assert state.mark_interrupted_runs(NOW) == 1
        row = state.get("ledgers", "c1")
        assert row.status == "pending_export" and row.records_last_run == 10
        assert state.last_run().status == "interrupted"
        assert state.recent_errors()[0].error == "boom"
