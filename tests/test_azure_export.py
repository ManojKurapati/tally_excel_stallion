from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select

from stallion_tally.azure.blob import BlobInfo, LocalBlobStorage, StorageAuthError, StorageError
from stallion_tally.azure.exporter import Exporter, export_dataset_names
from stallion_tally.azure.parquet import export_schema, row_to_record, write_parquet
from stallion_tally.config import EXPORT_DATASETS, Settings
from stallion_tally.database import Database
from stallion_tally.database.models import ExportBatchRow, LedgerRow
from stallion_tally.database.repositories import DatasetRepository, ExportRepository
from stallion_tally.models import Ledger
from stallion_tally.sync.normalize import normalize_ledger
from tests.conftest import NOW


def seed_ledgers(
    database: Database, n: int, company_id: str = "c1", company_name: str = "Acme & Co"
) -> None:
    rows = [
        normalize_ledger(
            Ledger(
                company_id=company_id,
                company_name=company_name,
                name=f"L{i:03d}",
                opening_balance=Decimal(i) / 3,
                raw_data={"i": i},
            )
        )
        for i in range(n)
    ]
    with database.session() as s:
        DatasetRepository(s, "ledgers").upsert(rows, "run-1", NOW)


def test_export_dataset_names_expands_vouchers() -> None:
    assert export_dataset_names(["vouchers", "ledgers"]) == [
        "ledgers",
        "vouchers",
        "voucher_ledger_entries",
        "voucher_inventory_entries",
    ]


def test_schema_covers_all_datasets() -> None:
    for dataset in EXPORT_DATASETS:
        schema = export_schema(dataset)
        assert (
            "company_id" in schema.names
            and "record_hash" in schema.names
            and "_exported_at" in schema.names
        )
        assert "export_status" not in schema.names
    assert "raw_json" not in export_schema("ledgers", include_raw_json=False).names


def test_parquet_roundtrip(tmp_path: Path, database: Database) -> None:
    seed_ledgers(database, 2)
    with database.session() as s:
        rows = s.execute(select(LedgerRow).order_by(LedgerRow.id)).scalars().all()
        records = [
            row_to_record(r, "ledgers", exported_at=NOW, batch_id="b1", run_id="r1") for r in rows
        ]
    written = write_parquet(tmp_path / "part-001.parquet", records, export_schema("ledgers"))
    assert written.record_count == 2 and written.size_bytes > 0
    assert written.sha256 == hashlib.sha256(written.path.read_bytes()).hexdigest()
    table = pq.read_table(written.path)
    assert table.schema.field("opening_balance").type == pa.decimal128(28, 6)
    data = table.to_pylist()
    assert data[1]["opening_balance"] == Decimal("0.333333")
    assert json.loads(data[1]["raw_json"]) == {"i": 1}
    assert data[0]["_batch_id"] == "b1" and data[0]["is_deleted"] is False


def test_local_storage_upload_and_properties(tmp_path: Path) -> None:
    storage = LocalBlobStorage(tmp_path / "container")
    storage.ensure_container()
    source = tmp_path / "file.bin"
    source.write_bytes(b"hello")
    md5 = hashlib.md5(b"hello").digest()
    info = storage.upload_file(
        "normalized/Acme/ledgers/2026-09-28/part-001.parquet",
        source,
        content_md5=md5,
        metadata={"sha256": "x"},
    )
    assert info.size_bytes == 5 and info.content_md5 == md5 and info.metadata == {"sha256": "x"}
    assert storage.get_properties("missing") is None
    with pytest.raises(StorageError):
        storage.upload_bytes("../escape.txt", b"x")


def test_exporter_creates_batches_manifests_and_marks_rows(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 7)
    exporter = Exporter(database, storage, settings, clock=lambda: NOW)
    summary = exporter.export_pending("run-1", datasets=["ledgers"])
    assert summary.ok and summary.verified == 3 and summary.records == 7  # batch size 3 -> 3,3,1
    files = [f.file_name for f in summary.files]
    assert files == ["part-001.parquet", "part-002.parquet", "part-003.parquet"]
    root = storage.root
    assert (root / "normalized/Acme & Co/ledgers/2026-09-28/part-002.parquet").exists()
    manifest = json.loads(
        (root / "manifests/Acme & Co/2026-09-28/ledgers-part-003.json").read_text()
    )
    assert (
        manifest["record_count"] == 1
        and manifest["company"] == "Acme & Co"
        and manifest["batch_id"]
    )
    assert (
        manifest["checksum"]
        == "sha256:"
        + hashlib.sha256(
            (root / "normalized/Acme & Co/ledgers/2026-09-28/part-003.parquet").read_bytes()
        ).hexdigest()
    )
    with database.session() as s:
        assert ExportRepository(s).pending_count("ledgers") == 0
        batches = s.execute(select(ExportBatchRow)).scalars().all()
        assert all(b.status == "verified" and b.verified_at is not None for b in batches)
        assert Path(batches[0].local_path).with_suffix(".manifest.json").exists()
    assert exporter.export_pending("run-2", datasets=["ledgers"]).files == []


def test_failed_upload_is_retried_from_existing_file(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 2)
    attempts = {"n": 0}

    class Failing:
        def describe(self):
            return "failing"

        def ensure_container(self):
            storage.ensure_container()

        def upload_bytes(self, *a, **k):
            return storage.upload_bytes(*a, **k)

        def get_properties(self, p):
            return storage.get_properties(p)

        def upload_file(self, *a, **k):
            attempts["n"] += 1
            raise StorageError("transient")

    exporter = Exporter(database, Failing(), settings, clock=lambda: NOW, sleep=lambda _s: None)  # type: ignore[arg-type]
    summary = exporter.export_pending("run-1", datasets=["ledgers"])
    assert summary.failed == 1 and attempts["n"] == settings.retry_max_attempts
    with database.session() as s:
        batch = s.execute(select(ExportBatchRow)).scalar_one()
        assert batch.status == "failed" and batch.last_error == "transient"
        rows = s.execute(select(LedgerRow)).scalars().all()
        assert all(
            r.export_status == "pending" and r.export_batch_id == batch.batch_id for r in rows
        )

    good = Exporter(database, storage, settings, clock=lambda: NOW)
    summary = good.export_pending("run-2", datasets=["ledgers"])
    assert summary.verified == 1 and summary.files[0].file_name == "part-001.parquet"
    with database.session() as s:
        assert s.execute(select(ExportBatchRow)).scalar_one().status == "verified"
        assert ExportRepository(s).pending_count("ledgers") == 0


def test_auth_errors_are_not_retried(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 1)
    attempts = {"n": 0}

    class Denied:
        def describe(self):
            return "denied"

        def ensure_container(self):
            storage.ensure_container()

        def upload_bytes(self, *a, **k):
            return storage.upload_bytes(*a, **k)

        def get_properties(self, p):
            return storage.get_properties(p)

        def upload_file(self, *a, **k):
            attempts["n"] += 1
            raise StorageAuthError("forbidden")

    summary = Exporter(
        database, Denied(), settings, clock=lambda: NOW, sleep=lambda _s: None
    ).export_pending("r", datasets=["ledgers"])  # type: ignore[arg-type]
    assert summary.failed == 1 and attempts["n"] == 1 and "forbidden" in summary.files[0].error


def test_container_failure_reports_error(settings: Settings, database: Database) -> None:
    class NoContainer:
        def describe(self):
            return "none"

        def ensure_container(self):
            raise StorageError("cannot create container")

        def upload_file(self, *a, **k):
            raise AssertionError

        def upload_bytes(self, *a, **k):
            raise AssertionError

        def get_properties(self, p):
            return None

    summary = Exporter(database, NoContainer(), settings).export_pending("r")  # type: ignore[arg-type]
    assert not summary.ok and "container" in summary.error
    assert summary.failed_for("c1", ["ledgers"]) == summary.error


def test_row_modified_after_batch_stays_pending(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 1)
    exporter = Exporter(database, storage, settings, clock=lambda: NOW)
    batch_id = exporter._create_batch("ledgers", "c1", "Acme & Co", "run-1")
    assert batch_id
    from datetime import timedelta

    with database.session() as s:
        DatasetRepository(s, "ledgers").upsert(
            [
                normalize_ledger(
                    Ledger(
                        company_id="c1",
                        company_name="Acme & Co",
                        name="L000",
                        opening_balance=Decimal("99"),
                    )
                )
            ],
            "run-2",
            NOW + timedelta(seconds=1),
        )
    result = exporter._process_batch(batch_id, "run-1")
    assert result.status == "verified"
    with database.session() as s:
        row = s.execute(select(LedgerRow)).scalar_one()
        assert row.export_status == "pending" and row.export_batch_id is None


def test_verification_detects_size_mismatch(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 1)

    class Truncating:
        def describe(self):
            return "trunc"

        def ensure_container(self):
            storage.ensure_container()

        def upload_bytes(self, *a, **k):
            return storage.upload_bytes(*a, **k)

        def get_properties(self, p):
            return storage.get_properties(p)

        def upload_file(self, blob_path, local_path, **k):
            info = storage.upload_file(blob_path, local_path, **k)
            return BlobInfo(info.path, info.size_bytes - 1, info.content_md5, info.metadata)

    summary = Exporter(
        database, Truncating(), settings, clock=lambda: NOW, sleep=lambda _s: None
    ).export_pending("r", datasets=["ledgers"])  # type: ignore[arg-type]
    assert summary.failed == 1 and "verification failed" in summary.files[0].error


def test_missing_local_file_releases_rows(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 1)
    exporter = Exporter(database, storage, settings, clock=lambda: NOW)
    batch_id = exporter._create_batch("ledgers", "c1", "Acme & Co", "run-1")
    with database.session() as s:
        Path(ExportRepository(s).get_batch(batch_id).local_path).unlink()
    summary = exporter.export_pending("run-2", datasets=["ledgers"])
    statuses = [f.status for f in summary.files]
    assert statuses == ["failed", "verified"]  # abandoned batch, then a fresh one
    with database.session() as s:
        assert {b.status for b in s.execute(select(ExportBatchRow)).scalars()} == {
            "abandoned",
            "verified",
        }
        assert ExportRepository(s).pending_count("ledgers") == 0


def test_manifest_content_md5_matches_blob(
    settings: Settings, database: Database, storage: LocalBlobStorage
) -> None:
    seed_ledgers(database, 1)
    Exporter(database, storage, settings, clock=lambda: NOW).export_pending(
        "r", datasets=["ledgers"]
    )
    manifest = json.loads(
        (storage.root / "manifests/Acme & Co/2026-09-28/ledgers-part-001.json").read_text()
    )
    info = storage.get_properties(manifest["blob_path"])
    assert base64.b64encode(info.content_md5).decode() == manifest["content_md5"]
    assert info.metadata["sha256"] == manifest["checksum"].split(":", 1)[1]


def test_settings_local_backend_dir_default(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, app_data_dir=tmp_path, azure_storage_backend="local")
    assert settings.local_backend_dir == tmp_path.resolve() / "azure_local"
    assert date.today()  # keep import used
