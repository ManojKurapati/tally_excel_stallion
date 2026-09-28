"""Application configuration.

All runtime configuration comes from environment variables or a local `.env`
file. Secrets are held in `SecretStr` values so they are never printed or
logged by accident.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SYNC_DATASETS: tuple[str, ...] = (
    "companies",
    "groups",
    "ledgers",
    "stock_items",
    "bills",
    "vouchers",
)
"""Datasets that can be requested from Tally, in pipeline order."""

EXPORT_DATASETS: tuple[str, ...] = SYNC_DATASETS + (
    "voucher_ledger_entries",
    "voucher_inventory_entries",
)
"""Datasets that are exported to Azure (vouchers are exploded into child tables)."""

ENV_FILE = os.environ.get("STALLION_TALLY_ENV_FILE", ".env")


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application -------------------------------------------------------
    app_env: Literal["development", "production"] = "production"
    app_data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_to_file: bool = True

    # --- tally ---------------------------------------------------------------
    tally_host: str = "localhost"
    tally_port: int = Field(default=9000, ge=1, le=65535)
    tally_timeout_seconds: float = Field(default=300, gt=0)
    tally_connect_timeout_seconds: float = Field(default=5, gt=0)
    tally_voucher_lookback_days: int = Field(default=90, ge=1)
    tally_voucher_chunk_size: int = Field(default=500, ge=1)
    tally_voucher_chunk_days: int = Field(default=7, ge=1)
    tally_companies: str = ""
    tally_unavailable_backoff_seconds: int = Field(default=30, ge=1)
    tally_unavailable_max_backoff_seconds: int = Field(default=600, ge=1)

    # --- persistence ---------------------------------------------------------
    database_url: str | None = None
    raw_retention_days: int = Field(default=14, ge=0)
    export_retention_days: int = Field(default=14, ge=0)

    # --- sync ----------------------------------------------------------------
    sync_interval_seconds: int = Field(default=300, ge=10)
    sync_full_refresh_interval_seconds: int = Field(default=3600, ge=0)
    sync_incremental_days: int = Field(default=7, ge=1)
    sync_datasets: str = ",".join(SYNC_DATASETS)

    # --- retry ---------------------------------------------------------------
    retry_max_attempts: int = Field(default=5, ge=1)
    retry_backoff_seconds: str = "2,5,15,30,60"

    # --- azure ---------------------------------------------------------------
    azure_storage_backend: Literal["azure", "local", "disabled"] = "azure"
    azure_storage_connection_string: SecretStr | None = None
    azure_storage_account_url: str | None = None
    azure_storage_container: str = "stallion-tally-data"
    azure_create_container: bool = True
    azure_upload_raw: bool = True
    azure_local_backend_dir: Path | None = None

    # --- export --------------------------------------------------------------
    export_batch_size: int = Field(default=500, ge=1)
    export_include_raw_json: bool = True

    # ------------------------------------------------------------------ validators
    @field_validator("azure_storage_connection_string", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("azure_storage_account_url", "database_url", mode="before")
    @classmethod
    def _empty_str_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("azure_local_backend_dir", mode="before")
    @classmethod
    def _empty_path_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported LOG_LEVEL {value!r}")
        return level

    @model_validator(mode="after")
    def _resolve_paths_and_check(self) -> Settings:
        self.app_data_dir = self.app_data_dir.expanduser().resolve()
        if self.azure_local_backend_dir is not None:
            self.azure_local_backend_dir = self.azure_local_backend_dir.expanduser().resolve()
        for name in self.dataset_list:
            if name not in SYNC_DATASETS:
                raise ValueError(
                    f"unknown dataset {name!r} in SYNC_DATASETS; valid: {', '.join(SYNC_DATASETS)}"
                )
        _ = self.backoff_schedule  # validate format
        if self.tally_unavailable_max_backoff_seconds < self.tally_unavailable_backoff_seconds:
            raise ValueError(
                "TALLY_UNAVAILABLE_MAX_BACKOFF_SECONDS must be >= TALLY_UNAVAILABLE_BACKOFF_SECONDS"
            )
        return self

    # ------------------------------------------------------------------ derived
    @property
    def tally_base_url(self) -> str:
        return f"http://{self.tally_host}:{self.tally_port}"

    @property
    def raw_dir(self) -> Path:
        return self.app_data_dir / "raw"

    @property
    def export_dir(self) -> Path:
        return self.app_data_dir / "exports"

    @property
    def log_dir(self) -> Path:
        return self.app_data_dir / "logs"

    @property
    def health_file(self) -> Path:
        return self.app_data_dir / "health.json"

    @property
    def db_path(self) -> Path:
        return self.app_data_dir / "local.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def local_backend_dir(self) -> Path:
        return self.azure_local_backend_dir or (self.app_data_dir / "azure_local")

    @property
    def company_allowlist(self) -> list[str]:
        return [c.strip() for c in self.tally_companies.split(",") if c.strip()]

    @property
    def dataset_list(self) -> list[str]:
        return [d.strip().lower() for d in self.sync_datasets.split(",") if d.strip()]

    @property
    def backoff_schedule(self) -> list[float]:
        try:
            values = [float(v) for v in self.retry_backoff_seconds.split(",") if v.strip()]
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError("RETRY_BACKOFF_SECONDS must be a comma separated list") from exc
        if not values:
            raise ValueError("RETRY_BACKOFF_SECONDS must not be empty")
        return values

    @property
    def export_enabled(self) -> bool:
        return self.azure_storage_backend != "disabled"

    # ------------------------------------------------------------------ helpers
    def ensure_directories(self) -> None:
        for path in (self.app_data_dir, self.raw_dir, self.export_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    def validation_problems(self) -> list[str]:
        """Return configuration problems that would stop a sync from succeeding."""
        problems: list[str] = []
        if self.azure_storage_backend == "azure":
            if not self.azure_storage_connection_string and not self.azure_storage_account_url:
                problems.append(
                    "AZURE_STORAGE_BACKEND=azure requires AZURE_STORAGE_CONNECTION_STRING "
                    "or AZURE_STORAGE_ACCOUNT_URL"
                )
            if self.azure_storage_account_url and not self.azure_storage_account_url.startswith(
                "https://"
            ):
                problems.append("AZURE_STORAGE_ACCOUNT_URL must use https://")
            if not self.azure_storage_container:
                problems.append("AZURE_STORAGE_CONTAINER must be set")
        if (
            self.tally_host not in {"localhost", "127.0.0.1", "::1"}
            and self.app_env == "production"
        ):
            problems.append(
                "TALLY_HOST should be localhost in production; "
                "the agent must run on the Tally machine"
            )
        return problems

    def describe(self) -> dict[str, Any]:
        """Configuration as a dictionary with secrets masked (safe to print/log)."""
        data: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                data[name] = "***" if value.get_secret_value() else None
            elif isinstance(value, Path):
                data[name] = str(value)
            else:
                data[name] = value
        data["resolved_database_url"] = self.resolved_database_url
        data["tally_base_url"] = self.tally_base_url
        return data


def get_settings(**overrides: Any) -> Settings:
    """Build settings from the environment (and `.env`), applying overrides."""
    return Settings(**overrides)
