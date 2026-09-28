from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from stallion_tally.azure.blob import StorageError
from stallion_tally.config import Settings
from stallion_tally.database import Database
from stallion_tally.database.models import (
    BillRow,
    ExportBatchRow,
    LedgerRow,
    RawFileRow,
    SyncErrorRow,
    VoucherLedgerEntryRow,
    VoucherRow,
)
from stallion_tally.sync.checkpoint import plan_voucher_window
from stallion_tally.sync.manager import SyncManager
from stallion_tally.sync.scheduler import Agent
from stallion_tally.sync.state import ALL_COMPANIES, SyncStateRepository
from stallion_tally.tally.client import TallyClient
from tests.conftest import COMPANY_1, COMPANY_1_ID, NOW, TODAY, FakeTally

SALES_GUID = f"{COMPANY_1_ID}-00000101"
PAYMENT_GUID = f"{COMPANY_1_ID}-00000102"


def by_dataset(result, dataset: str, company: str = COMPANY_1):
    return next(r for r in result.results if r.dataset == dataset and r.company_name == company)


def count(db: Database, model: type, **where) -> int:
    with db.session() as s:
        stmt = select(func.count()).select_from(model)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        return s.execute(stmt).scalar_one()


def test_end_to_end_sync(
    manager: SyncManager, database: Database, settings: Settings, fake_tally: FakeTally
) -> None:
    result = manager.run()
    assert result.status == "completed", result.summary()
    assert result.companies == [COMPANY_1, "Stallion Parts & Service"]

    companies = next(r for r in result.results if r.dataset == "companies")
    assert companies.inserted == 2 and companies.status == "completed"
    assert by_dataset(result, "ledgers").inserted == 3
    assert by_dataset(result, "ledgers").rejected == 1
    assert by_dataset(result, "groups").inserted == 3
    assert by_dataset(result, "stock_items").inserted == 2
    assert by_dataset(result, "bills").inserted == 2
    vouchers = by_dataset(result, "vouchers")
    assert vouchers.inserted == 4  # the May voucher is outside the 90 day lookback
    assert vouchers.window_from == "2026-06-30" and vouchers.window_to == "2026-09-28"
    assert vouchers.requests == 13  # 91 days in 7-day chunks
    assert by_dataset(result, "ledgers", "Stallion Parts & Service").inserted == 0

    assert count(database, VoucherRow) == 4
    assert count(database, VoucherLedgerEntryRow) == 6
    assert count(database, LedgerRow, export_status="pending") == 0

    with database.session() as s:
        states = {(r.dataset, r.company_id): r for r in SyncStateRepository(s).all()}
    assert states[("vouchers", COMPANY_1_ID)].status == "completed"
    assert states[("companies", ALL_COMPANIES)].status == "completed"
    assert states[("vouchers", COMPANY_1_ID)].last_export is not None

    cloud = settings.local_backend_dir / settings.azure_storage_container
    parquet_files = sorted(p.relative_to(cloud).as_posix() for p in cloud.rglob("*.parquet"))
    assert f"normalized/{COMPANY_1}/ledgers/2026-09-28/part-001.parquet" in parquet_files
    assert f"normalized/{COMPANY_1}/vouchers/2026-09-28/part-001.parquet" in parquet_files
    assert (
        f"normalized/{COMPANY_1}/vouchers/2026-09-28/part-002.parquet" in parquet_files
    )  # batch size 3
    assert (
        f"normalized/{COMPANY_1}/voucher_ledger_entries/2026-09-28/part-001.parquet"
        in parquet_files
    )
    assert f"normalized/{COMPANY_1}/companies/2026-09-28/part-001.parquet" in parquet_files
    manifests = list(cloud.rglob("manifests/**/*.json"))
    assert manifests
    manifest = json.loads(next(m for m in manifests if "ledgers" in m.name).read_text())
    assert manifest["record_count"] == 3 and manifest["status"] == "success"
    assert manifest["checksum"].startswith("sha256:")
    raw_files = list(cloud.rglob("raw/**/*.xml"))
    assert len(raw_files) == count(database, RawFileRow, uploaded=True) > 0

    table = pq.read_table(cloud / f"normalized/{COMPANY_1}/vouchers/2026-09-28/part-001.parquet")
    assert (
        table.num_rows == 3
        and "tally_guid" in table.column_names
        and "raw_json" in table.column_names
    )

    assert settings.health_file.exists()
    assert json.loads(settings.health_file.read_text())["status"] == "completed"
    assert any(settings.raw_dir.rglob("vouchers_*.xml"))


def test_sync_is_idempotent(manager: SyncManager, database: Database, settings: Settings) -> None:
    first = manager.run()
    second = manager.run()
    third = manager.run()
    assert first.status == second.status == third.status == "completed"
    assert (
        count(database, VoucherRow) == 4
        and count(database, LedgerRow) == 3
        and count(database, BillRow) == 2
    )
    for dataset in ("ledgers", "groups", "stock_items", "bills", "vouchers"):
        r = by_dataset(third, dataset)
        assert (r.inserted, r.updated, r.deleted) == (0, 0, 0), dataset
        assert r.unchanged == by_dataset(first, dataset).inserted
    assert second.export.verified == 0 and third.export.verified == 0
    assert count(database, ExportBatchRow) == first.export.verified


def test_change_and_deletion_detection(
    manager: SyncManager, database: Database, fake_tally: FakeTally, settings: Settings
) -> None:
    manager.run()
    fake_tally.set_narration(SALES_GUID, "Amended narration")
    fake_tally.drop_ledger_entry(PAYMENT_GUID)
    fake_tally.remove_voucher(f"{COMPANY_1_ID}-00000104")
    fake_tally.masters["Ledgers"] = fake_tally.masters["Ledgers"].replace(
        b'<LEDGER NAME="Cash" RESERVEDNAME="Cash">', b'<LEDGER NAME="Cash Renamed" RESERVEDNAME="">'
    )
    result = manager.run()
    assert result.status == "completed", result.summary()
    vouchers = by_dataset(result, "vouchers")
    assert vouchers.updated == 2 and vouchers.deleted == 1 and vouchers.unchanged == 1
    ledgers = by_dataset(result, "ledgers")
    assert ledgers.inserted == 1 and ledgers.deleted == 1
    with database.session() as s:
        deleted = s.execute(
            select(VoucherRow).where(VoucherRow.tally_guid == f"{COMPANY_1_ID}-00000104")
        ).scalar_one()
        assert deleted.is_deleted is True and deleted.export_status == "exported"
        entries = (
            s.execute(
                select(VoucherLedgerEntryRow).where(
                    VoucherLedgerEntryRow.voucher_guid == PAYMENT_GUID
                )
            )
            .scalars()
            .all()
        )
        assert {e.line_no: e.is_deleted for e in entries} == {1: False, 2: True}
        cash = s.execute(select(LedgerRow).where(LedgerRow.name == "Cash")).scalar_one()
        assert cash.is_deleted is True
    cloud = settings.local_backend_dir / settings.azure_storage_container
    part2 = pq.read_table(
        cloud / f"normalized/{COMPANY_1}/vouchers/2026-09-28/part-003.parquet"
    ).to_pylist()
    assert {r["tally_guid"]: r["is_deleted"] for r in part2}[f"{COMPANY_1_ID}-00000104"] is True


def test_tally_unavailable_is_handled(
    manager: SyncManager, database: Database, fake_tally: FakeTally
) -> None:
    fake_tally.available = False
    result = manager.run()
    assert result.status == "tally_unavailable"
    assert result.results == []
    assert count(database, SyncErrorRow, operation="tally_check") == 1
    with database.session() as s:
        assert SyncStateRepository(s).last_run().status == "tally_unavailable"


def test_tally_lost_mid_run(manager: SyncManager, fake_tally: FakeTally) -> None:
    original = fake_tally.handler
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] > 3:
            fake_tally.available = False
        return original(request)

    manager.client._client._transport = __import__("httpx").MockTransport(flaky)
    result = manager.run()
    assert result.status == "tally_unavailable"
    assert any(r.status == "failed" for r in result.results)


def test_export_failure_keeps_sync_incomplete_and_is_retried(
    settings: Settings, database: Database, tally_client: TallyClient, storage, clock
) -> None:
    class FlakyStorage:
        def __init__(self) -> None:
            self.fail = True

        def describe(self) -> str:
            return "flaky"

        def ensure_container(self) -> None:
            storage.ensure_container()

        def upload_file(self, *args, **kwargs):
            if self.fail:
                raise StorageError("network down")
            return storage.upload_file(*args, **kwargs)

        def upload_bytes(self, *args, **kwargs):
            return storage.upload_bytes(*args, **kwargs)

        def get_properties(self, path):
            return storage.get_properties(path)

    flaky = FlakyStorage()
    manager = SyncManager(settings, database, tally_client, flaky, clock=clock)  # type: ignore[arg-type]
    result = manager.run(datasets=["ledgers"])
    assert result.status == "partial"  # second company had nothing to export
    assert by_dataset(result, "ledgers").status == "export_failed"
    assert result.export.failed >= 1
    with database.session() as s:
        state = SyncStateRepository(s).get("ledgers", COMPANY_1_ID)
        assert (
            state.status == "export_failed"
            and state.last_export is None
            and "network down" in state.last_error
        )
        batch = s.execute(select(ExportBatchRow)).scalars().first()
        assert batch.status == "failed" and batch.attempts == 1
    assert count(database, LedgerRow, export_status="pending") == 3

    flaky.fail = False
    result = manager.run(datasets=["ledgers"])
    assert result.status == "completed"
    assert count(database, LedgerRow, export_status="pending") == 0
    with database.session() as s:
        batches = s.execute(select(ExportBatchRow)).scalars().all()
        assert {b.status for b in batches} == {"verified"}
        assert len(batches) == 1  # the failed batch file was reused, no duplicate batch


def test_no_export_flag_leaves_datasets_pending(manager: SyncManager, database: Database) -> None:
    result = manager.run(datasets=["ledgers"], export=False)
    assert result.status == "completed_no_export"
    with database.session() as s:
        assert SyncStateRepository(s).get("ledgers", COMPANY_1_ID).status == "pending_export"
    assert count(database, LedgerRow, export_status="pending") == 3


def test_manual_window_pulls_old_voucher(manager: SyncManager, database: Database) -> None:
    result = manager.run(
        datasets=["vouchers"], from_date=date(2026, 4, 1), to_date=date(2026, 9, 28)
    )
    assert by_dataset(result, "vouchers").inserted == 5
    assert by_dataset(result, "vouchers").window_from == "2026-04-01"


def test_company_filters(manager: SyncManager, settings: Settings, fake_tally: FakeTally) -> None:
    result = manager.run(datasets=["groups"], companies=["stallion parts & service"])
    assert result.companies == ["Stallion Parts & Service"]
    settings.tally_companies = COMPANY_1
    result = manager.run(datasets=["groups"])
    assert result.companies == [COMPANY_1]


def test_bills_fall_back_to_reports(
    manager: SyncManager, database: Database, fake_tally: FakeTally
) -> None:
    fake_tally.bills_collection_error = True
    result = manager.run(datasets=["bills"])
    assert result.status == "completed"
    assert by_dataset(result, "bills").inserted == 3
    with database.session() as s:
        types = {r.bill_ref: r.bill_type for r in s.execute(select(BillRow)).scalars()}
    assert types == {
        "SI/2026/0091": "receivable",
        "SI/2026/0040": "receivable",
        "PI-778": "payable",
    }
    assert any(r["report"] == "Bills Payable" for r in fake_tally.requests)


def test_validation_errors_are_recorded(manager: SyncManager, database: Database) -> None:
    manager.run(datasets=["ledgers", "vouchers"])
    with database.session() as s:
        errors = (
            s.execute(select(SyncErrorRow).where(SyncErrorRow.operation == "validate"))
            .scalars()
            .all()
        )
    assert any("BROKEN-NO-GUID" in e.error for e in errors)
    assert any(e.dataset == "ledgers" for e in errors)


def test_rejected_record_is_not_treated_as_deleted(
    manager: SyncManager, database: Database, fake_tally: FakeTally
) -> None:
    manager.run(datasets=["vouchers"])
    # Break a previously valid voucher (unparseable date) -> rejected, but it must stay un-deleted.
    fake_tally.day_book_tree.find(f".//VOUCHER[GUID='{PAYMENT_GUID}']/DATE").text = "not-a-date"
    result = manager.run(datasets=["vouchers"])
    vouchers = by_dataset(result, "vouchers")
    assert vouchers.rejected >= 1 and vouchers.deleted == 0
    with database.session() as s:
        row = s.execute(
            select(VoucherRow).where(VoucherRow.tally_guid == PAYMENT_GUID)
        ).scalar_one()
        assert row.is_deleted is False


def test_interrupted_run_is_marked_on_restart(manager: SyncManager, database: Database) -> None:
    with database.session() as s:
        SyncStateRepository(s).start_run("agent", ["ledgers"], NOW)
    with database.session() as s:
        assert SyncStateRepository(s).mark_interrupted_runs(NOW) == 1
    result = manager.run(datasets=["ledgers"])
    assert result.status == "completed"


def test_agent_waits_for_tally_then_syncs(
    settings: Settings,
    manager: SyncManager,
    tally_client: TallyClient,
    fake_tally: FakeTally,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tally.available = False
    waits: list[float] = []

    def fake_wait(self: Agent, seconds: float) -> bool:
        waits.append(seconds)
        fake_tally.available = True
        return False

    monkeypatch.setattr(Agent, "_wait", fake_wait)
    agent = Agent(settings, manager, tally_client, install_signal_handlers=False)
    result = agent.run_forever(max_cycles=1)
    assert waits == [settings.tally_unavailable_backoff_seconds]
    assert result is not None and result.status == "completed" and result.trigger == "agent"


def test_agent_stops_on_request(
    settings: Settings, manager: SyncManager, tally_client: TallyClient
) -> None:
    agent = Agent(settings, manager, tally_client, install_signal_handlers=False)
    agent.stop()
    assert agent.run_forever() is None


# --- window planning ---------------------------------------------------------


def _plan(**kwargs):
    base = dict(
        today=TODAY,
        now=NOW,
        checkpoint=None,
        lookback_days=90,
        incremental_days=7,
        full_refresh_interval_seconds=3600,
        books_from=date(2025, 4, 1),
    )
    base.update(kwargs)
    return plan_voucher_window(**base)


def test_plan_window_defaults_to_full_lookback() -> None:
    window = _plan()
    assert (window.from_date, window.to_date, window.mode) == (date(2026, 6, 30), TODAY, "full")


def test_plan_window_incremental_when_recently_refreshed() -> None:
    checkpoint = {"last_full_refresh_at": datetime(2026, 9, 28, 9, 30, tzinfo=UTC).isoformat()}
    window = _plan(checkpoint=checkpoint, scheduled=True)
    assert window.mode == "incremental" and window.from_date == date(2026, 9, 21)
    stale = {"last_full_refresh_at": datetime(2026, 9, 28, 8, 0, tzinfo=UTC).isoformat()}
    assert _plan(checkpoint=stale, scheduled=True).mode == "full"
    assert _plan(checkpoint=checkpoint, scheduled=False).mode == "full"


def test_plan_window_clamps_to_books_from_and_manual() -> None:
    assert _plan(books_from=date(2026, 8, 1)).from_date == date(2026, 8, 1)
    manual = _plan(from_date=date(2026, 1, 1), to_date=date(2026, 1, 31))
    assert manual.mode == "manual" and manual.to_date == date(2026, 1, 31)


def test_scheduled_runs_use_incremental_window_after_full(manager: SyncManager, clock) -> None:
    first = manager.run(datasets=["vouchers"], trigger="agent")
    assert by_dataset(first, "vouchers").requests == 13
    clock.advance(minutes=5)
    second = manager.run(datasets=["vouchers"], trigger="agent")
    assert by_dataset(second, "vouchers").requests == 2  # 8 days -> two 7-day chunks
    assert by_dataset(second, "vouchers").deleted == 0
    clock.advance(hours=2)
    third = manager.run(datasets=["vouchers"], trigger="agent")
    assert by_dataset(third, "vouchers").requests == 13
