"""Blob storage backends.

`AzureBlobStorage` talks to Azure Blob Storage. `LocalBlobStorage` writes the
same layout to a local folder so the whole pipeline can be exercised without
Azure credentials (tests, first installation checks).
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class StorageError(Exception):
    """Storage operation failed (retryable unless stated otherwise)."""

    retryable: bool = True


class StorageAuthError(StorageError):
    retryable = False


class StorageConfigError(StorageError):
    retryable = False


@dataclass(frozen=True)
class BlobInfo:
    path: str
    size_bytes: int
    content_md5: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class BlobStorage(Protocol):
    def describe(self) -> str: ...

    def ensure_container(self) -> None: ...

    def upload_file(
        self,
        blob_path: str,
        local_path: Path,
        *,
        content_md5: bytes | None = None,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> BlobInfo: ...

    def upload_bytes(
        self,
        blob_path: str,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> BlobInfo: ...

    def get_properties(self, blob_path: str) -> BlobInfo | None: ...


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------


class LocalBlobStorage:
    """Filesystem backend that mimics the blob container layout."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> str:
        return f"local folder {self.root}"

    def ensure_container(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _target(self, blob_path: str) -> Path:
        target = (self.root / blob_path).resolve()
        if self.root.resolve() not in target.parents:
            raise StorageConfigError(f"blob path escapes container: {blob_path}")
        return target

    def _write_sidecar(
        self,
        target: Path,
        metadata: dict[str, str] | None,
        content_md5: bytes | None,
        content_type: str | None,
    ) -> None:
        sidecar = target.with_name(target.name + ".meta.json")
        sidecar.write_text(
            json.dumps(
                {
                    "metadata": metadata or {},
                    "content_md5": base64.b64encode(content_md5).decode() if content_md5 else None,
                    "content_type": content_type,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def upload_file(
        self,
        blob_path: str,
        local_path: Path,
        *,
        content_md5: bytes | None = None,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> BlobInfo:
        target = self._target(blob_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        shutil.copyfile(local_path, tmp)
        tmp.replace(target)
        if content_md5 is None:
            content_md5 = hashlib.md5(target.read_bytes()).digest()  # noqa: S324
        self._write_sidecar(target, metadata, content_md5, content_type)
        info = self.get_properties(blob_path)
        assert info is not None
        return info

    def upload_bytes(
        self,
        blob_path: str,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> BlobInfo:
        target = self._target(blob_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._write_sidecar(target, metadata, hashlib.md5(data).digest(), content_type)  # noqa: S324
        info = self.get_properties(blob_path)
        assert info is not None
        return info

    def get_properties(self, blob_path: str) -> BlobInfo | None:
        target = self._target(blob_path)
        if not target.exists():
            return None
        sidecar = target.with_name(target.name + ".meta.json")
        metadata: dict[str, str] = {}
        content_md5: bytes | None = None
        if sidecar.exists():
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            metadata = meta.get("metadata") or {}
            if meta.get("content_md5"):
                content_md5 = base64.b64decode(meta["content_md5"])
        return BlobInfo(blob_path, target.stat().st_size, content_md5, metadata)


# ---------------------------------------------------------------------------
# Azure backend
# ---------------------------------------------------------------------------


def _wrap_azure_error(exc: Exception) -> StorageError:
    from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

    if isinstance(exc, ClientAuthenticationError):
        return StorageAuthError(f"Azure authentication failed: {exc.message}")
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return StorageAuthError(f"Azure denied the request (HTTP {status}): {exc.message}")
        return StorageError(f"Azure request failed (HTTP {status}): {exc.message}")
    return StorageError(f"Azure request failed: {exc}")


class AzureBlobStorage:
    """Azure Blob Storage backend built on `azure-storage-blob`."""

    def __init__(
        self, container_client: Any, *, create_container: bool = True, account_label: str = ""
    ) -> None:
        self._container = container_client
        self._create_container = create_container
        self._label = account_label

    def describe(self) -> str:
        return (
            f"Azure Blob Storage container '{self._container.container_name}' {self._label}".strip()
        )

    def ensure_container(self) -> None:
        from azure.core.exceptions import AzureError, ResourceExistsError

        try:
            if self._create_container:
                try:
                    self._container.create_container()
                except ResourceExistsError:
                    pass
            elif not self._container.exists():
                raise StorageConfigError(
                    f"container '{self._container.container_name}' does not exist"
                )
        except AzureError as exc:
            raise _wrap_azure_error(exc) from exc

    def upload_file(
        self,
        blob_path: str,
        local_path: Path,
        *,
        content_md5: bytes | None = None,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> BlobInfo:
        from azure.core.exceptions import AzureError
        from azure.storage.blob import ContentSettings

        try:
            blob = self._container.get_blob_client(blob_path)
            with local_path.open("rb") as fh:
                blob.upload_blob(
                    fh,
                    overwrite=True,
                    metadata=metadata or {},
                    content_settings=ContentSettings(
                        content_type=content_type, content_md5=content_md5
                    ),
                    max_concurrency=2,
                )
        except AzureError as exc:
            raise _wrap_azure_error(exc) from exc
        info = self.get_properties(blob_path)
        if info is None:
            raise StorageError(f"blob {blob_path} missing after upload")
        return info

    def upload_bytes(
        self,
        blob_path: str,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> BlobInfo:
        from azure.core.exceptions import AzureError
        from azure.storage.blob import ContentSettings

        try:
            blob = self._container.get_blob_client(blob_path)
            blob.upload_blob(
                data,
                overwrite=True,
                metadata=metadata or {},
                content_settings=ContentSettings(
                    content_type=content_type,
                    content_md5=hashlib.md5(data).digest(),  # noqa: S324
                ),
            )
        except AzureError as exc:
            raise _wrap_azure_error(exc) from exc
        info = self.get_properties(blob_path)
        if info is None:
            raise StorageError(f"blob {blob_path} missing after upload")
        return info

    def get_properties(self, blob_path: str) -> BlobInfo | None:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            props = self._container.get_blob_client(blob_path).get_blob_properties()
        except ResourceNotFoundError:
            return None
        except AzureError as exc:
            raise _wrap_azure_error(exc) from exc
        md5 = props.content_settings.content_md5 if props.content_settings else None
        return BlobInfo(
            blob_path,
            int(props.size),
            bytes(md5) if md5 else None,
            dict(props.metadata or {}),
        )
