from __future__ import annotations

from pathlib import Path

import pytest

from stallion_tally.config import SYNC_DATASETS, Settings


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.tally_base_url == "http://localhost:9000"
    assert settings.tally_voucher_lookback_days == 90
    assert settings.tally_voucher_chunk_size == 500
    assert settings.sync_interval_seconds == 300
    assert settings.dataset_list == list(SYNC_DATASETS)
    assert settings.backoff_schedule == [2, 5, 15, 30, 60]
    assert settings.resolved_database_url.endswith("/data/local.db")
    assert settings.app_data_dir.is_absolute()


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TALLY_PORT", "9100")
    monkeypatch.setenv("TALLY_COMPANIES", "Alpha Ltd, Beta Ltd")
    monkeypatch.setenv(
        "AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=https;AccountKey=secret"
    )
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "custom"))
    settings = Settings(_env_file=None)
    assert settings.tally_port == 9100
    assert settings.company_allowlist == ["Alpha Ltd", "Beta Ltd"]
    assert settings.app_data_dir == (tmp_path / "custom").resolve()
    assert settings.validation_problems() == []


def test_describe_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "AccountKey=supersecret")
    described = Settings(_env_file=None).describe()
    assert described["azure_storage_connection_string"] == "***"
    assert "supersecret" not in str(described)


def test_azure_backend_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    settings = Settings(_env_file=None, azure_storage_backend="azure")
    problems = settings.validation_problems()
    assert any("AZURE_STORAGE_CONNECTION_STRING" in p for p in problems)


def test_managed_identity_url_must_be_https() -> None:
    settings = Settings(
        _env_file=None, azure_storage_account_url="http://acct.blob.core.windows.net"
    )
    assert any("https" in p for p in settings.validation_problems())


def test_invalid_dataset_rejected() -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        Settings(_env_file=None, sync_datasets="ledgers,unicorns")


def test_local_backend_needs_no_credentials() -> None:
    settings = Settings(_env_file=None, azure_storage_backend="local")
    assert settings.validation_problems() == []
    assert settings.export_enabled
