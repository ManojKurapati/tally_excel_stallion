"""Export pending local records to blob storage as Parquet parts with manifests.

Flow per (company, dataset):

    pending rows -> Parquet part on disk -> export_batches row
      -> upload -> verify (size + Content-MD5) -> manifest -> rows marked exported

A batch whose upload fails stays on disk with status `failed` and is retried
by the next export before new batches are created.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from stallion_tally import __version__
from stallion_tally.azure.blob import BlobInfo, BlobStorage, StorageError
from stallion_tally.azure.parquet import export_schema, row_to_record, write_parquet
from stallion_tally.config import EXPORT_DATASETS, Settings
from stallion_tally.database import Database
from stallion_tally.database.models import ExportBatchRow
from stallion_tally.database.repositories import ExportRepository
from stallion_tally.logging import get_logger
from stallion_tally.utils.dates import utcnow
from stallion_tally.utils.retry import RetryExhausted, retry
from stallion_tally.utils.text import safe_name

log = get_logger(__name__)

MANIFEST_SCHEMA_VERSION = 1
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


@dataclass
class ExportFileResult:
    company_id: str
    company_name: str
    dataset: str
    batch_id: str
    file_name: str
    blob_path: str
    record_count: int
    status: str  # verified | failed
    error: str | None = None


@dataclass
class ExportSummary:
    files: list[ExportFileResult] = field(default_factory=list)
    raw_uploaded: int = 0
    raw_failed: int = 0
    error: str | None = None

    @property
    def verified(self) -> int:
        return sum(1 for f in self.files if f.status == "verified")

    @property
    def failed(self) -> int:
        return sum(1 for f in self.files if f.status == "failed")

    @property
    def records(self) -> int:
        return sum(f.record_count for f in self.files if f.status == "verified")

    @property
    def ok(self) -> bool:
        return self.error is None and self.failed == 0 and self.raw_failed == 0

    def failed_for(self, company_id: str, datasets: Iterable[str]) -> str | None:
        wanted = set(datasets)
        if self.error:
            return self.error
        for f in self.files:
            if f.status == "failed" and f.company_id == company_id and f.dataset in wanted:
                return f.error or "upload failed"
        return None


def export_dataset_names(sync_datasets: Iterable[str]) -> list[str]:
    """Map sync datasets to the export datasets they produce (vouchers -> children)."""
    names: list[str] = []
    for name in sync_datasets:
        names.append(name)
        if name == "vouchers":
            names.extend(["voucher_ledger_entries", "voucher_inventory_entries"])
    return [d for d in EXPORT_DATASETS if d in names]


class Exporter:
    def __init__(
        self,
        database: Database,
        storage: BlobStorage,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = database
        self.storage = storage
        self.settings = settings
        self.clock = clock
        self.sleep = sleep

    # ------------------------------------------------------------------ public
    def export_pending(
        self,
        run_id: str | None = None,
        *,
        company_ids: Iterable[str] | None = None,
        datasets: Iterable[str] | None = None,
    ) -> ExportSummary:
        summary = ExportSummary()
        dataset_names = list(datasets) if datasets is not None else list(EXPORT_DATASETS)
        company_filter = set(company_ids) if company_ids is not None else None
        try:
            self.storage.ensure_container()
        except StorageError as exc:
            log.error(
                "Storage container is not available",
                error=str(exc),
                storage=self.storage.describe(),
            )
            summary.error = str(exc)
            return summary

        # 1. retry batches that did not complete earlier
        with self.db.session() as session:
            incomplete = ExportRepository(session).incomplete_batches(company_filter, dataset_names)
            batch_ids = [b.batch_id for b in incomplete]
        for batch_id in batch_ids:
            summary.files.append(self._process_batch(batch_id, run_id))

        # 2. new batches for pending rows
        for dataset in dataset_names:
            with self.db.session() as session:
                companies = ExportRepository(session).pending_companies(dataset)
            for company_id, company_name in companies:
                if company_filter is not None and company_id not in company_filter:
                    continue
                while True:
                    batch_id = self._create_batch(dataset, company_id, company_name, run_id)
                    if batch_id is None:
                        break
                    result = self._process_batch(batch_id, run_id)
                    summary.files.append(result)
                    if result.status != "verified":
                        break  # keep remaining rows pending; retry next time

        # 3. raw responses
        if self.settings.azure_upload_raw:
            uploaded, failed = self._upload_raw_files(company_filter)
            summary.raw_uploaded += uploaded
            summary.raw_failed += failed
        return summary

    # ------------------------------------------------------------------ batches
    def _create_batch(
        self, dataset: str, company_id: str, company_name: str, run_id: str | None
    ) -> str | None:
        now = self.clock()
        export_date = now.date().isoformat()
        include_raw = self.settings.export_include_raw_json
        with self.db.session() as session:
            repo = ExportRepository(session)
            rows = repo.fetch_pending(dataset, company_id, self.settings.export_batch_size)
            if not rows:
                return None
            part = repo.next_part_number(company_id, dataset, export_date)
            file_name = f"part-{part:03d}.parquet"
            company_dir = safe_name(company_name)
            local_path = self.settings.export_dir / company_dir / dataset / export_date / file_name
            blob_path = f"normalized/{company_dir}/{dataset}/{export_date}/{file_name}"
            batch = repo.create_batch(
                run_id=run_id,
                company_id=company_id,
                company_name=company_name,
                dataset=dataset,
                export_date=export_date,
                part_number=part,
                record_count=len(rows),
                file_name=file_name,
                local_path=str(local_path),
                blob_path=blob_path,
                manifest_blob_path=f"manifests/{company_dir}/{export_date}/{dataset}-part-{part:03d}.json",
                status="created",
                created_at=now,
            )
            records = [
                row_to_record(
                    row,
                    dataset,
                    exported_at=now,
                    batch_id=batch.batch_id,
                    run_id=run_id,
                    include_raw_json=include_raw,
                )
                for row in rows
            ]
            written = write_parquet(local_path, records, export_schema(dataset, include_raw))
            batch.checksum_sha256 = written.sha256
            batch.content_md5 = base64.b64encode(written.md5).decode()
            batch.size_bytes = written.size_bytes
            repo.assign_batch(dataset, [row.id for row in rows], batch.batch_id)
            batch_id = batch.batch_id
        log.info(
            "Export batch created",
            dataset=dataset,
            company=company_name,
            file=file_name,
            records=len(rows),
            size_bytes=written.size_bytes,
        )
        return batch_id

    def _process_batch(self, batch_id: str, run_id: str | None) -> ExportFileResult:
        with self.db.session() as session:
            batch = ExportRepository(session).get_batch(batch_id)
            assert batch is not None
            snapshot = _BatchSnapshot.from_row(batch)
        local_path = Path(snapshot.local_path)
        if not local_path.exists():
            with self.db.session() as session:
                repo = ExportRepository(session)
                batch = repo.get_batch(batch_id)
                assert batch is not None
                repo.release_batch(batch)
                batch.status = "abandoned"
                batch.last_error = "local file missing; rows released for re-export"
            log.warning(
                "Export batch file missing, rows released",
                batch_id=batch_id,
                file=snapshot.local_path,
            )
            return snapshot.result("failed", "local export file missing; rows will be re-exported")

        try:
            info = self._upload_with_retry(snapshot, local_path)
            self._verify(snapshot, info)
            manifest_bytes = self._manifest(snapshot, run_id, "success")
            self.storage.upload_bytes(
                snapshot.manifest_blob_path,
                manifest_bytes,
                content_type="application/json",
                metadata={"dataset": snapshot.dataset, "batch_id": snapshot.batch_id},
            )
            local_path.with_suffix(".manifest.json").write_bytes(manifest_bytes)
        except (StorageError, RetryExhausted, OSError) as exc:
            error = str(exc.last_error) if isinstance(exc, RetryExhausted) else str(exc)
            with self.db.session() as session:
                batch = ExportRepository(session).get_batch(batch_id)
                assert batch is not None
                batch.status = "failed"
                batch.attempts += 1
                batch.last_error = error[:4000]
            log.error(
                "Azure upload failed",
                company=snapshot.company_name,
                dataset=snapshot.dataset,
                file=snapshot.file_name,
                attempt=snapshot.attempts + 1,
                error=error,
            )
            return snapshot.result("failed", error)

        now = self.clock()
        with self.db.session() as session:
            repo = ExportRepository(session)
            batch = repo.get_batch(batch_id)
            assert batch is not None
            batch.status = "verified"
            batch.attempts += 1
            batch.uploaded_at = now
            batch.verified_at = now
            batch.last_error = None
            exported = repo.mark_batch_exported(batch, now)
        log.info(
            "Azure upload successful",
            company=snapshot.company_name,
            dataset=snapshot.dataset,
            file=snapshot.file_name,
            blob=snapshot.blob_path,
            records=exported,
        )
        return snapshot.result("verified")

    def _upload_with_retry(self, snapshot: _BatchSnapshot, local_path: Path) -> BlobInfo:
        md5 = base64.b64decode(snapshot.content_md5) if snapshot.content_md5 else None
        metadata = {
            "sha256": snapshot.checksum_sha256 or "",
            "record_count": str(snapshot.record_count),
            "dataset": snapshot.dataset,
            "company_id": snapshot.company_id,
            "batch_id": snapshot.batch_id,
        }

        def on_retry(attempt: int, error: BaseException, delay: float) -> None:
            log.warning(
                "Azure upload failed, retrying",
                dataset=snapshot.dataset,
                file=snapshot.file_name,
                attempt=attempt,
                retry_in_seconds=delay,
                error=str(error),
            )

        def non_retryable(exc: BaseException) -> bool:
            return isinstance(exc, StorageError) and not exc.retryable

        def operation() -> BlobInfo:
            try:
                return self.storage.upload_file(
                    snapshot.blob_path,
                    local_path,
                    content_md5=md5,
                    metadata=metadata,
                    content_type=PARQUET_CONTENT_TYPE,
                )
            except StorageError as exc:
                if non_retryable(exc):
                    raise _NonRetryable(exc) from exc
                raise

        try:
            return retry(
                operation,
                attempts=self.settings.retry_max_attempts,
                schedule=self.settings.backoff_schedule,
                retry_on=(StorageError, OSError),
                non_retryable=(_NonRetryable,),
                on_retry=on_retry,
                sleep=self.sleep,
            )
        except _NonRetryable as exc:
            raise exc.error from exc

    @staticmethod
    def _verify(snapshot: _BatchSnapshot, info: BlobInfo) -> None:
        if snapshot.size_bytes is not None and info.size_bytes != snapshot.size_bytes:
            raise StorageError(
                f"upload verification failed: size {info.size_bytes} != {snapshot.size_bytes}"
            )
        if snapshot.content_md5 and info.content_md5 is not None:
            expected = base64.b64decode(snapshot.content_md5)
            if info.content_md5 != expected:
                raise StorageError("upload verification failed: Content-MD5 mismatch")

    def _manifest(self, snapshot: _BatchSnapshot, run_id: str | None, status: str) -> bytes:
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "agent_version": __version__,
            "company": snapshot.company_name,
            "company_id": snapshot.company_id,
            "dataset": snapshot.dataset,
            "exported_at": self.clock().isoformat(timespec="seconds").replace("+00:00", "Z"),
            "record_count": snapshot.record_count,
            "file": snapshot.file_name,
            "blob_path": snapshot.blob_path,
            "format": "parquet",
            "checksum": f"sha256:{snapshot.checksum_sha256}",
            "content_md5": snapshot.content_md5,
            "size_bytes": snapshot.size_bytes,
            "batch_id": snapshot.batch_id,
            "run_id": run_id,
            "status": status,
        }
        return json.dumps(manifest, indent=2).encode("utf-8")

    # ------------------------------------------------------------------ raw files
    def _upload_raw_files(self, company_filter: set[str] | None) -> tuple[int, int]:
        uploaded = failed = 0
        with self.db.session() as session:
            pending = [
                (r.id, r.local_path, r.blob_path, r.checksum_sha256, r.dataset)
                for r in ExportRepository(session).pending_raw_files(company_filter)
            ]
        for row_id, local, blob_path, checksum, dataset in pending:
            path = Path(local)
            error: str | None = None
            if not path.exists():
                error = "raw file missing"
            else:
                try:
                    self.storage.upload_file(
                        blob_path,
                        path,
                        metadata={"sha256": checksum or "", "dataset": dataset},
                        content_type="application/xml",
                    )
                except StorageError as exc:
                    error = str(exc)
            with self.db.session() as session:
                from stallion_tally.database.models import RawFileRow

                row = session.get(RawFileRow, row_id)
                if row is None:
                    continue
                row.attempts += 1
                if error is None:
                    row.uploaded = True
                    row.uploaded_at = self.clock()
                    row.last_error = None
                    uploaded += 1
                else:
                    row.last_error = error[:4000]
                    if error == "raw file missing":
                        session.delete(row)
                    failed += 1
        if uploaded or failed:
            log.info("Raw responses uploaded", uploaded=uploaded, failed=failed)
        return uploaded, failed


class _NonRetryable(Exception):
    def __init__(self, error: StorageError) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True)
class _BatchSnapshot:
    batch_id: str
    company_id: str
    company_name: str
    dataset: str
    file_name: str
    local_path: str
    blob_path: str
    manifest_blob_path: str
    record_count: int
    checksum_sha256: str | None
    content_md5: str | None
    size_bytes: int | None
    attempts: int

    @classmethod
    def from_row(cls, row: ExportBatchRow) -> _BatchSnapshot:
        return cls(
            batch_id=row.batch_id,
            company_id=row.company_id,
            company_name=row.company_name,
            dataset=row.dataset,
            file_name=row.file_name,
            local_path=row.local_path,
            blob_path=row.blob_path,
            manifest_blob_path=row.manifest_blob_path or "",
            record_count=row.record_count,
            checksum_sha256=row.checksum_sha256,
            content_md5=row.content_md5,
            size_bytes=row.size_bytes,
            attempts=row.attempts,
        )

    def result(self, status: str, error: str | None = None) -> ExportFileResult:
        return ExportFileResult(
            company_id=self.company_id,
            company_name=self.company_name,
            dataset=self.dataset,
            batch_id=self.batch_id,
            file_name=self.file_name,
            blob_path=self.blob_path,
            record_count=self.record_count,
            status=status,
            error=error,
        )


__all__ = ["ExportFileResult", "ExportSummary", "Exporter", "export_dataset_names"]
