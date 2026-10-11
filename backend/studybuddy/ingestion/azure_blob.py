"""Azure Blob Storage implementation of `ObjectStore` (LLD 04 s4): uploaded files live in a private container.

Two ways to authenticate: a connection string (local Azurite, or a storage account key) or an account URL with
`DefaultAzureCredential` (managed identity in Azure; needs the "Storage Blob Data Contributor" role on the account).
Keys are `tenant/course/document/version/...` paths and are used as blob names unchanged.
"""
from __future__ import annotations

from typing import Any


class AzureBlobObjectStore:
    def __init__(self, container_client: Any, *, owns: list | None = None):
        self.container = container_client
        self._owns = owns or []   # clients to close with the store

    @classmethod
    def from_connection_string(cls, conn_str: str, container: str) -> "AzureBlobObjectStore":
        from azure.storage.blob.aio import BlobServiceClient
        svc = BlobServiceClient.from_connection_string(conn_str)
        return cls(svc.get_container_client(container), owns=[svc])

    @classmethod
    def from_account_url(cls, account_url: str, container: str, credential: Any = None) -> "AzureBlobObjectStore":
        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient
        cred = credential or DefaultAzureCredential()
        svc = BlobServiceClient(account_url, credential=cred)
        return cls(svc.get_container_client(container), owns=[svc] + ([cred] if credential is None else []))

    async def ensure_container(self) -> None:
        """Create the container (private: no public access) if it is missing."""
        from azure.core.exceptions import ResourceExistsError
        try:
            await self.container.create_container()
        except ResourceExistsError:
            pass

    async def put(self, key: str, data: bytes) -> None:
        await self.container.upload_blob(key, bytes(data), overwrite=True)

    async def get(self, key: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            stream = await self.container.download_blob(key)
            return await stream.readall()
        except ResourceNotFoundError:
            raise FileNotFoundError(key) from None

    async def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            await self.container.delete_blob(key)
        except ResourceNotFoundError:
            pass

    async def close(self) -> None:
        for c in self._owns:
            await c.close()
