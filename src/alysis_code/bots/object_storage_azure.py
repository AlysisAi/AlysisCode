"""Azure Blob adapter. Only the controller receives the storage credential.

An existing private container and a TokenCredential are supplied by the operator;
this adapter creates no accounts, containers, permissions or public URLs.
"""

from urllib.parse import urlsplit

from ..cancellation import CooperativeCancellationError
from .files import FILE_CHUNK_BYTES
from .object_storage import ObjectStorage, ObjectStorageError


class AzureBlobStorage(ObjectStorage):
    def __init__(self, account_url, container, credential, *, emulator=False):
        from azure.storage.blob import ContainerClient

        url = urlsplit(account_url)
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (url.scheme != "https" and not (emulator and local and url.scheme == "http"))
            or (emulator and not local)
        ):
            raise ValueError("Use HTTPS storage or an explicit loopback emulator.")
        if not emulator and not callable(getattr(credential, "get_token", None)):
            raise ValueError("Cloud storage requires a controller token credential.")
        self._client = ContainerClient(
            account_url,
            container,
            credential=credential,
            retry_total=0,
            connection_timeout=5,
            read_timeout=15,
            max_single_put_size=4 * 1024 * 1024,
            max_block_size=4 * 1024 * 1024,
            max_single_get_size=FILE_CHUNK_BYTES,
            logging_enable=False,
        )

    def close(self):
        self._client.close()

    def _upload(self, ref, stream):
        try:
            self._client.upload_blob(
                ref.key,
                stream,
                length=ref.size,
                overwrite=False,
                validate_content=True,
                max_concurrency=1,
                timeout=60,
            )
        except CooperativeCancellationError:
            raise
        except Exception:
            raise ObjectStorageError("Cloud object upload failed; no file was published.") from None

    def delete(self, ref):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._client.delete_blob(ref.key, timeout=15)
        except ResourceNotFoundError:
            pass
        except Exception:
            raise ObjectStorageError("Cloud object deletion could not be confirmed.") from None

    def _range(self, ref, offset, length):
        try:
            if ref.size == 0:
                properties = self._client.get_blob_client(ref.key).get_blob_properties(timeout=15)
                if properties.size != 0:
                    raise ObjectStorageError("Stored object size changed.")
                return b""
            return self._client.download_blob(
                ref.key,
                offset=offset,
                length=length,
                validate_content=True,
                max_concurrency=1,
                timeout=15,
            ).readall()
        except Exception:
            raise ObjectStorageError("Cloud object is unavailable.") from None
