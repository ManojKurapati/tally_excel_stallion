"""Retention of raw responses and local export files."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from stallion_tally.config import Settings
from stallion_tally.database import Database
from stallion_tally.database.models import ExportBatchRow
from stallion_tally.database.repositories import ExportRepository
from stallion_tally.logging import get_logger
from stallion_tally.utils.dates import utcnow

log = get_logger(__name__)


def _remove(path: Path) -> bool:
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        log.warning("Could not delete file", path=str(path), error=str(exc))
    return False


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def cleanup(settings: Settings, database: Database, now: datetime | None = None) -> dict[str, int]:
    """Delete raw and export files older than the configured retention."""
    now = now or utcnow()
    removed_raw = removed_exports = 0
    raw_needed_in_cloud = settings.azure_storage_backend == "azure" and settings.azure_upload_raw

    if settings.raw_retention_days > 0:
        cutoff = now - timedelta(days=settings.raw_retention_days)
        with database.session() as session:
            repo = ExportRepository(session)
            rows = (
                repo.uploaded_raw_files_before(cutoff)
                if raw_needed_in_cloud
                else [
                    r
                    for r in repo.pending_raw_files() + repo.uploaded_raw_files_before(cutoff)
                    if r.created_at <= cutoff
                ]
            )
            for row in rows:
                if _remove(Path(row.local_path)):
                    removed_raw += 1
                repo.delete_raw_file(row)
        # Orphan files not tracked in the database (e.g. interrupted runs).
        if settings.raw_dir.exists():
            for path in settings.raw_dir.rglob("*.xml*"):
                if datetime.fromtimestamp(
                    path.stat().st_mtime, tz=now.tzinfo
                ) <= cutoff and _remove(path):
                    removed_raw += 1
        _prune_empty_dirs(settings.raw_dir)

    if settings.export_retention_days > 0:
        cutoff = now - timedelta(days=settings.export_retention_days)
        with database.session() as session:
            stmt = select(ExportBatchRow).where(
                ExportBatchRow.status == "verified", ExportBatchRow.verified_at <= cutoff
            )
            for batch in session.execute(stmt).scalars():
                local = Path(batch.local_path)
                if _remove(local):
                    removed_exports += 1
                _remove(local.with_suffix(".manifest.json"))
        _prune_empty_dirs(settings.export_dir)

    if removed_raw or removed_exports:
        log.info(
            "Housekeeping removed old files", raw_files=removed_raw, export_files=removed_exports
        )
    return {"raw_files": removed_raw, "export_files": removed_exports}
