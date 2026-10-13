from __future__ import annotations

from collections.abc import Iterator
from typing import IO, Any

USAGE_EXPORT_CONTAINER = "turnstile-usage-exports"


class PrivateUsageExportBlob:
    """Managed-identity adapter for the private usage-export container."""

    def __init__(
        self,
        account_url: str,
        *,
        blob_service_client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self._credential = credential
        self._service = blob_service_client
        if self._service is None:
            if not account_url.strip():
                raise ValueError("Usage export Blob endpoint is not configured")
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            self._credential = credential or DefaultAzureCredential()
            self._service = BlobServiceClient(
                account_url=account_url.rstrip("/"), credential=self._credential
            )
        self._container = self._service.get_container_client(USAGE_EXPORT_CONTAINER)

    def upload(self, name: str, body: IO[bytes]) -> None:
        body.seek(0)
        self._container.upload_blob(name, body, overwrite=True)

    def delete(self, name: str) -> None:
        try:
            self._container.delete_blob(name)
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise

    def download_chunks(self, name: str, chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
        downloader = self._container.download_blob(
            name, max_concurrency=1, max_chunk_get_size=chunk_size
        )
        yield from downloader.chunks()

    def close(self) -> None:
        close_service = getattr(self._service, "close", None)
        if callable(close_service):
            close_service()
        close_credential = getattr(self._credential, "close", None)
        if callable(close_credential):
            close_credential()
