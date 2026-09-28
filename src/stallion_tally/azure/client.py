"""Azure authentication and storage backend construction.

Development: `AZURE_STORAGE_CONNECTION_STRING`.
Production: `AZURE_STORAGE_ACCOUNT_URL` + Managed Identity (DefaultAzureCredential).
Secrets are read from settings only; nothing is hard-coded or logged.
"""

from __future__ import annotations

from typing import Any

from stallion_tally.azure.blob import (
    AzureBlobStorage,
    BlobStorage,
    LocalBlobStorage,
    StorageConfigError,
)
from stallion_tally.config import Settings
from stallion_tally.logging import get_logger

log = get_logger(__name__)


def build_blob_service_client(settings: Settings) -> Any:
    from azure.storage.blob import BlobServiceClient

    if settings.azure_storage_connection_string:
        return BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string.get_secret_value(),
            retry_total=3,
            connection_timeout=30,
        )
    if settings.azure_storage_account_url:
        from azure.identity import DefaultAzureCredential

        return BlobServiceClient(
            account_url=settings.azure_storage_account_url,
            credential=DefaultAzureCredential(),
            retry_total=3,
            connection_timeout=30,
        )
    raise StorageConfigError(
        "set AZURE_STORAGE_CONNECTION_STRING (development) or "
        "AZURE_STORAGE_ACCOUNT_URL with Managed Identity (production)"
    )


def build_storage(settings: Settings) -> BlobStorage | None:
    """Create the configured storage backend, or None when export is disabled."""
    backend = settings.azure_storage_backend
    if backend == "disabled":
        return None
    if backend == "local":
        root = settings.local_backend_dir / settings.azure_storage_container
        log.info("Using local storage backend", path=str(root))
        return LocalBlobStorage(root)
    service = build_blob_service_client(settings)
    container = service.get_container_client(settings.azure_storage_container)
    label = (
        f"({settings.azure_storage_account_url})"
        if settings.azure_storage_account_url
        else "(connection string)"
    )
    return AzureBlobStorage(
        container, create_container=settings.azure_create_container, account_label=label
    )
