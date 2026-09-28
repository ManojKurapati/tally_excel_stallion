"""Synchronisation manager: the end-to-end pipeline for one run.

    check Tally -> discover companies -> extract -> save raw -> parse -> validate
    -> normalize -> upsert SQLite -> export batches -> upload -> verify -> state

A dataset is only marked `completed` once its export succeeded; otherwise the
local data stays available and the state is `export_failed` for retry.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from itertools import islice
from typing import Any

from stallion_tally.azure.blob import BlobStorage
from stallion_tally.azure.exporter import Exporter, ExportSummary, export_dataset_names
from stallion_tally.config import Settings
from stallion_tally.database import Database
from stallion_tally.database.repositories import (
    BillRepository,
    DatasetRepository,
    ExportRepository,
    UpsertResult,
    VoucherRepository,
)
from stallion_tally.logging import get_logger
from stallion_tally.models import Company
from stallion_tally.sync import housekeeping
from stallion_tally.sync.checkpoint import (
    VOUCHER_WINDOW_KEY,
    CheckpointRepository,
    plan_voucher_window,
    voucher_checkpoint_value,
)
from stallion_tally.sync.normalize import (
    normalize_bill,
    normalize_company,
    normalize_group,
    normalize_ledger,
    normalize_stock_item,
    normalize_voucher,
)
from stallion_tally.sync.pull import Puller, RawFile
from stallion_tally.sync.state import ALL_COMPANIES, SyncStateRepository
from stallion_tally.tally.client import TallyClient
from stallion_tally.tally.datasets import DatasetSpec, resolve_datasets
from stallion_tally.tally.exceptions import TallyError, TallyUnavailableError
from stallion_tally.utils.dates import date_windows, utcnow

log = get_logger(__name__)

MASTER_NORMALIZERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "groups": normalize_group,
    "ledgers": normalize_ledger,
    "stock_items": normalize_stock_item,
}


def batched(iterable: Iterable[Any], size: int) -> Iterator[list[Any]]:
    iterator = iter(iterable)
    while chunk := list(islice(iterator, size)):
        yield chunk


@dataclass
class DatasetResult:
    dataset: str
    company_id: str
    company_name: str
    status: str = "pending"
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    rejected: int = 0
    requests: int = 0
    window_from: str | None = None
    window_to: str | None = None
    error: str | None = None

    @property
    def records(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def apply(self, result: UpsertResult) -> None:
        self.inserted += result.inserted
        self.updated += result.updated
        self.unchanged += result.unchanged
        self.deleted += result.deleted
        self.rejected += result.rejected


@dataclass
class SyncRunResult:
    run_id: str
    started_at: datetime
    trigger: str
    finished_at: datetime | None = None
    status: str = "running"
    message: str | None = None
    companies: list[str] = field(default_factory=list)
    results: list[DatasetResult] = field(default_factory=list)
    export: ExportSummary | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "status": self.status,
            "message": self.message,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "companies": self.companies,
            "datasets": [asdict(r) for r in self.results],
            "export": None
            if self.export is None
            else {
                "files_verified": self.export.verified,
                "files_failed": self.export.failed,
                "records": self.export.records,
                "raw_uploaded": self.export.raw_uploaded,
                "raw_failed": self.export.raw_failed,
                "error": self.export.error,
            },
        }


class SyncManager:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        client: TallyClient,
        storage: BlobStorage | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.db = database
        self.client = client
        self.storage = storage
        self.clock = clock
        self.exporter = Exporter(database, storage, settings, clock=clock) if storage else None

    # ------------------------------------------------------------------ public
    def run(
        self,
        *,
        datasets: Iterable[str] | None = None,
        companies: Iterable[str] | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        export: bool = True,
        trigger: str = "cli",
    ) -> SyncRunResult:
        specs = resolve_datasets(datasets)
        requested = [s.name for s in specs]
        started = self.clock()
        with self.db.session() as session:
            run_row = SyncStateRepository(session).start_run(trigger, requested, started)
            run_id = run_row.run_id
        result = SyncRunResult(run_id=run_id, started_at=started, trigger=trigger)
        log.info("Sync run started", run_id=run_id, trigger=trigger, datasets=",".join(requested))

        # 1. Tally availability
        try:
            info = self.client.server_info()
        except TallyError as exc:
            self._record_error(run_id, None, None, "tally_check", str(exc))
            log.error("Tally is not available", error=str(exc), url=self.client.base_url)
            return self._finish(result, "tally_unavailable", str(exc))
        log.info("Tally connection successful", server=info)

        puller = Puller(self.client, self.settings.raw_dir, clock=self.clock)

        # 2. Company discovery
        try:
            found, raw, report = puller.pull_companies()
        except TallyError as exc:
            self._record_error(run_id, None, "companies", "pull", str(exc))
            log.error("Company discovery failed", error=str(exc))
            return self._finish(result, "failed", f"company discovery failed: {exc}")
        self._register_raw(run_id, raw)
        for company in found:
            log.info("Company discovered", company=company.name, company_id=company.company_id)
        selected = self._select_companies(found, companies)
        result.companies = [c.name for c in selected]

        if "companies" in requested:
            result.results.append(
                self._sync_companies(run_id, found, report.rejected, report.errors)
            )
        if not selected:
            log.warning(
                "No companies to synchronise (none loaded in Tally or none match the allow-list)"
            )
            return self._finish(result, "no_companies", "no companies selected", export=export)

        # 3. Per-company datasets
        tally_lost = False
        for company in selected:
            for spec in specs:
                if not spec.per_company:
                    continue
                dataset_result = DatasetResult(spec.name, company.company_id, company.name)
                result.results.append(dataset_result)
                self._mark_started(spec.name, company)
                try:
                    self._sync_dataset(
                        puller, company, spec, run_id, dataset_result, from_date, to_date, trigger
                    )
                    self._mark_pulled(spec.name, company, dataset_result.records)
                    dataset_result.status = "pulled"
                except TallyUnavailableError as exc:
                    self._fail_dataset(run_id, company, spec.name, dataset_result, "pull", exc)
                    tally_lost = True
                    break
                except Exception as exc:  # noqa: BLE001 - every failure is recorded
                    self._fail_dataset(run_id, company, spec.name, dataset_result, "sync", exc)
            if tally_lost:
                break

        # 4. Export
        self._export(result, run_id, selected, requested, export)

        # 5. Housekeeping
        try:
            housekeeping.cleanup(self.settings, self.db, self.clock())
        except Exception as exc:  # noqa: BLE001
            log.warning("Housekeeping failed", error=str(exc))

        status = self._overall_status(result, tally_lost)
        return self._finish(result, status)

    # ------------------------------------------------------------------ datasets
    def _sync_dataset(
        self,
        puller: Puller,
        company: Company,
        spec: DatasetSpec,
        run_id: str,
        dataset_result: DatasetResult,
        from_date: date | None,
        to_date: date | None,
        trigger: str,
    ) -> None:
        log.info("Extracting dataset", dataset=spec.name, company=company.name)
        if spec.kind == "master":
            self._sync_master(puller, company, spec.name, run_id, dataset_result)
        elif spec.kind == "snapshot":
            self._sync_bills(puller, company, run_id, dataset_result)
        elif spec.kind == "transaction":
            self._sync_vouchers(
                puller, company, run_id, dataset_result, from_date, to_date, trigger
            )
        else:  # pragma: no cover - registry guards this
            raise ValueError(f"unsupported dataset kind {spec.kind}")
        log.info(
            "Local upsert successful",
            dataset=spec.name,
            company=company.name,
            inserted=dataset_result.inserted,
            updated=dataset_result.updated,
            unchanged=dataset_result.unchanged,
            deleted=dataset_result.deleted,
            rejected=dataset_result.rejected,
        )

    def _sync_companies(
        self, run_id: str, found: list[Company], rejected: int, errors: list[str]
    ) -> DatasetResult:
        dataset_result = DatasetResult("companies", ALL_COMPANIES, "all companies")
        self._mark_started("companies", None)
        try:
            with self.db.session() as session:
                repo = DatasetRepository(session, "companies")
                dataset_result.apply(
                    repo.upsert([normalize_company(c) for c in found], run_id, self.clock())
                )
            dataset_result.rejected = rejected
            for error in errors:
                self._record_error(run_id, None, "companies", "validate", error)
            self._mark_pulled("companies", None, dataset_result.records)
            dataset_result.status = "pulled"
            log.info("Parsed records", dataset="companies", records=dataset_result.records)
        except Exception as exc:  # noqa: BLE001
            self._fail_dataset(run_id, None, "companies", dataset_result, "persist", exc)
        return dataset_result

    def _sync_master(
        self,
        puller: Puller,
        company: Company,
        dataset: str,
        run_id: str,
        dataset_result: DatasetResult,
    ) -> None:
        records, raw, report = puller.pull_masters(company, dataset)
        dataset_result.requests += 1
        self._register_raw(run_id, raw)
        normalize = MASTER_NORMALIZERS[dataset]
        now = self.clock()
        with self.db.session() as session:
            repo = DatasetRepository(session, dataset)
            for chunk in batched(records, self.settings.tally_voucher_chunk_size):
                dataset_result.apply(repo.upsert([normalize(r) for r in chunk], run_id, now))
            self._report_rejections(session, run_id, company, dataset, report, dataset_result)
            repo.touch(report.rejected_keys, run_id)
            dataset_result.deleted += len(
                repo.mark_missing_deleted(company.company_id, run_id, now=now)
            )
        log.info("Parsed records", dataset=dataset, company=company.name, records=report.parsed)

    def _sync_bills(
        self, puller: Puller, company: Company, run_id: str, dataset_result: DatasetResult
    ) -> None:
        bills, raw_files, report = puller.pull_bills(company)
        dataset_result.requests += len(raw_files)
        for raw in raw_files:
            self._register_raw(run_id, raw)
        now = self.clock()
        with self.db.session() as session:
            repo = BillRepository(session)
            for chunk in batched(bills, self.settings.tally_voucher_chunk_size):
                dataset_result.apply(
                    repo.bills.upsert([normalize_bill(b, now) for b in chunk], run_id, now)
                )
            self._report_rejections(session, run_id, company, "bills", report, dataset_result)
            repo.bills.touch(report.rejected_keys, run_id)
            dataset_result.deleted += repo.mark_missing_settled(company.company_id, run_id, now)
        log.info("Parsed records", dataset="bills", company=company.name, records=report.parsed)

    def _sync_vouchers(
        self,
        puller: Puller,
        company: Company,
        run_id: str,
        dataset_result: DatasetResult,
        from_date: date | None,
        to_date: date | None,
        trigger: str,
    ) -> None:
        now = self.clock()
        with self.db.session() as session:
            checkpoint = CheckpointRepository(session).get(
                "vouchers", company.company_id, VOUCHER_WINDOW_KEY
            )
        window = plan_voucher_window(
            today=now.date(),
            now=now,
            checkpoint=checkpoint,
            lookback_days=self.settings.tally_voucher_lookback_days,
            incremental_days=self.settings.sync_incremental_days,
            full_refresh_interval_seconds=self.settings.sync_full_refresh_interval_seconds,
            books_from=company.books_from,
            from_date=from_date,
            to_date=to_date,
            scheduled=trigger == "agent",
        )
        dataset_result.window_from = window.from_date.isoformat()
        dataset_result.window_to = window.to_date.isoformat()
        log.info(
            "Voucher window",
            company=company.name,
            from_date=window.from_date.isoformat(),
            to_date=window.to_date.isoformat(),
            mode=window.mode,
        )
        for seq, (chunk_from, chunk_to) in enumerate(
            date_windows(window.from_date, window.to_date, self.settings.tally_voucher_chunk_days),
            start=1,
        ):
            vouchers, raw, report = puller.pull_day_book(company, chunk_from, chunk_to, seq)
            dataset_result.requests += 1
            self._register_raw(run_id, raw)
            batch_now = self.clock()
            with self.db.session() as session:
                repo = VoucherRepository(session)
                for chunk in batched(vouchers, self.settings.tally_voucher_chunk_size):
                    voucher_rows, ledger_rows, inventory_rows = [], [], []
                    for voucher in chunk:
                        row, ledger, inventory = normalize_voucher(voucher)
                        voucher_rows.append(row)
                        ledger_rows.extend(ledger)
                        inventory_rows.extend(inventory)
                    dataset_result.apply(
                        repo.upsert(voucher_rows, ledger_rows, inventory_rows, run_id, batch_now)
                    )
                self._report_rejections(
                    session, run_id, company, "vouchers", report, dataset_result
                )
                repo.vouchers.touch(report.rejected_keys, run_id)
            log.info(
                "Parsed records",
                dataset="vouchers",
                company=company.name,
                records=report.parsed,
                from_date=chunk_from.isoformat(),
                to_date=chunk_to.isoformat(),
            )
        with self.db.session() as session:
            dataset_result.deleted += VoucherRepository(session).mark_missing_deleted(
                company.company_id, run_id, window.from_date, window.to_date, self.clock()
            )
            CheckpointRepository(session).set(
                "vouchers",
                company.company_id,
                VOUCHER_WINDOW_KEY,
                voucher_checkpoint_value(checkpoint, window, run_id, self.clock()),
            )

    # ------------------------------------------------------------------ export
    def _export(
        self,
        result: SyncRunResult,
        run_id: str,
        selected: list[Company],
        requested: list[str],
        export: bool,
    ) -> None:
        pulled = [r for r in result.results if r.status == "pulled"]
        if not export:
            log.info("Export skipped (--no-export); datasets stay pending_export")
            return
        if self.exporter is None:
            for dataset_result in pulled:
                self._mark_completed(dataset_result)
            log.info("Export disabled by configuration; local sync marked completed")
            return
        company_ids = [c.company_id for c in selected]
        summary = self.exporter.export_pending(
            run_id, company_ids=company_ids, datasets=export_dataset_names(requested)
        )
        result.export = summary
        if summary.error:
            self._record_error(run_id, None, None, "export", summary.error)
        for dataset_result in pulled:
            targets = export_dataset_names([dataset_result.dataset])
            if dataset_result.company_id == ALL_COMPANIES:
                error = next(
                    (e for cid in company_ids if (e := summary.failed_for(cid, targets))),
                    summary.error,
                )
            else:
                error = summary.failed_for(dataset_result.company_id, targets)
            if error:
                dataset_result.status = "export_failed"
                dataset_result.error = error
                with self.db.session() as session:
                    SyncStateRepository(session).mark_failed(
                        dataset_result.dataset,
                        dataset_result.company_id,
                        dataset_result.company_name,
                        error,
                        status="export_failed",
                        now=self.clock(),
                    )
            else:
                self._mark_completed(dataset_result)

    # ------------------------------------------------------------------ helpers
    def _select_companies(
        self, found: list[Company], requested: Iterable[str] | None
    ) -> list[Company]:
        allowlist = {c.lower() for c in self.settings.company_allowlist}
        wanted = {c.lower() for c in requested} if requested else None
        selected: list[Company] = []
        for company in found:
            name = company.name.lower()
            if allowlist and name not in allowlist:
                log.info("Company skipped (not in TALLY_COMPANIES)", company=company.name)
                continue
            if (
                wanted is not None
                and name not in wanted
                and company.company_id.lower() not in wanted
            ):
                continue
            selected.append(company)
        if wanted:
            missing = (
                wanted
                - {c.name.lower() for c in selected}
                - {c.company_id.lower() for c in selected}
            )
            for name in missing:
                log.warning("Requested company is not loaded in Tally", company=name)
        return selected

    def _register_raw(self, run_id: str, raw: RawFile) -> None:
        with self.db.session() as session:
            ExportRepository(session).add_raw_file(
                run_id=run_id,
                company_id=raw.company.company_id if raw.company else None,
                company_name=raw.company.name if raw.company else None,
                dataset=raw.dataset,
                local_path=str(raw.path),
                blob_path=raw.blob_path,
                size_bytes=raw.size_bytes,
                checksum_sha256=raw.checksum_sha256,
                uploaded=False,
                created_at=self.clock(),
            )

    def _report_rejections(
        self,
        session: Any,
        run_id: str,
        company: Company,
        dataset: str,
        report: Any,
        dataset_result: DatasetResult,
    ) -> None:
        """Record validation rejections using the caller's session (SQLite allows one writer)."""
        if report.rejected:
            dataset_result.rejected += report.rejected
            log.warning(
                "Records rejected by validation",
                dataset=dataset,
                company=company.name,
                rejected=report.rejected,
                first_error=report.errors[0] if report.errors else None,
            )
            state = SyncStateRepository(session)
            for error in report.errors:
                state.record_error(
                    run_id=run_id,
                    company_id=company.company_id,
                    dataset=dataset,
                    operation="validate",
                    error=error,
                    now=self.clock(),
                )

    def _record_error(
        self,
        run_id: str | None,
        company_id: str | None,
        dataset: str | None,
        operation: str,
        error: str,
        retry_count: int = 0,
    ) -> None:
        with self.db.session() as session:
            SyncStateRepository(session).record_error(
                run_id=run_id,
                company_id=company_id,
                dataset=dataset,
                operation=operation,
                error=error,
                retry_count=retry_count,
                now=self.clock(),
            )

    def _mark_started(self, dataset: str, company: Company | None) -> None:
        with self.db.session() as session:
            SyncStateRepository(session).mark_started(
                dataset,
                company.company_id if company else ALL_COMPANIES,
                company.name if company else None,
                self.clock(),
            )

    def _mark_pulled(self, dataset: str, company: Company | None, records: int) -> None:
        with self.db.session() as session:
            SyncStateRepository(session).mark_pulled(
                dataset,
                company.company_id if company else ALL_COMPANIES,
                company.name if company else None,
                records,
                self.clock(),
            )

    def _mark_completed(self, dataset_result: DatasetResult) -> None:
        dataset_result.status = "completed"
        with self.db.session() as session:
            SyncStateRepository(session).mark_completed(
                dataset_result.dataset, dataset_result.company_id, self.clock()
            )

    def _fail_dataset(
        self,
        run_id: str,
        company: Company | None,
        dataset: str,
        dataset_result: DatasetResult,
        operation: str,
        exc: BaseException,
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"
        dataset_result.status = "failed"
        dataset_result.error = error
        log.error(
            "Dataset sync failed",
            dataset=dataset,
            company=company.name if company else None,
            operation=operation,
            error=error,
            exc_info=not isinstance(exc, TallyError),
        )
        self._record_error(
            run_id, company.company_id if company else None, dataset, operation, error
        )
        with self.db.session() as session:
            SyncStateRepository(session).mark_failed(
                dataset,
                company.company_id if company else ALL_COMPANIES,
                company.name if company else None,
                error,
                now=self.clock(),
            )

    @staticmethod
    def _overall_status(result: SyncRunResult, tally_lost: bool) -> str:
        if tally_lost:
            return "tally_unavailable"
        statuses = {r.status for r in result.results}
        if not statuses:
            return "completed"
        if statuses <= {"completed"}:
            return "completed"
        if statuses <= {"completed", "pulled"}:
            return "completed_no_export"
        if statuses & {"completed", "pulled"}:
            return "partial"
        return "failed"

    def _finish(
        self, result: SyncRunResult, status: str, message: str | None = None, export: bool = True
    ) -> SyncRunResult:
        result.status = status
        result.message = message
        result.finished_at = self.clock()
        if status == "no_companies" and export:
            # Companies dataset may still need exporting.
            self._export(result, result.run_id, [], ["companies"], export)
        summary = result.summary()
        with self.db.session() as session:
            SyncStateRepository(session).finish_run(
                result.run_id, status, summary, message, result.finished_at
            )
        self._write_health(summary)
        level = log.info if status in {"completed", "completed_no_export"} else log.warning
        level(
            "Sync run finished",
            run_id=result.run_id,
            status=status,
            datasets=len(result.results),
            message=message,
        )
        return result

    def _write_health(self, summary: dict[str, Any]) -> None:
        try:
            self.settings.health_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings.health_file.write_text(
                json.dumps(summary, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover
            log.warning("Could not write health file", error=str(exc))
