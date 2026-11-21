"""A small client for the owls HTTP service (``src/alysis_code/bots/app.py``).

Every request carries ``Authorization: Bearer <token>``, the service's own credential.
The client only creates its own owls, sends them messages, reads what they did and
downloads their Library files. It never answers a card or a question.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

# The service's transfer limits (bots/files.py): whole-file transfers up to 2 MiB, larger
# files in 256 KiB chunks.
INLINE_FILE_BYTES = 2 * 1024 * 1024
FILE_CHUNK_BYTES = 256 * 1024
PAGE_LIMIT = 100


class OwlsError(RuntimeError):
    """The owls service refused a request."""

    def __init__(self, status: int, detail: str, path: str):
        super().__init__(f"{path} answered HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.path = path


def request_id() -> str:
    return uuid.uuid4().hex


class OwlsClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 60.0,
        opener: Callable | None = None,
    ):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("The owls URL must be an http(s) address.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Give the owls URL without credentials, query or fragment.")
        if not token:
            raise ValueError("The owls service needs its token (--owls-token or the environment).")
        self.base = url.rstrip("/")
        self._token = token
        self.timeout_s = timeout_s
        self._open = opener or urllib.request.urlopen

    def describe(self) -> dict:
        """What a report may record about the service: never the token."""
        parts = urlsplit(self.base)
        return {"host": parts.hostname, "port": parts.port, "scheme": parts.scheme}

    def request(self, method: str, path: str, body=None):
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method in ("POST", "PUT", "PATCH"):
            # The service requires a Content-Length on every write, even an empty one.
            data = b""
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with self._open(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except (ValueError, AttributeError, OSError):
                detail = None
            raise OwlsError(exc.code, str(detail or exc.reason), path) from None
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    # Service ---------------------------------------------------------------------------

    def status(self) -> dict:
        return self.request("GET", "/v1/status")

    def models(self) -> list[dict]:
        return (self.request("GET", "/v1/models") or {}).get("models", [])

    def needs_you(self) -> list[dict]:
        return (self.request("GET", "/v1/needs-you") or {}).get("items", [])

    # Owls and chats --------------------------------------------------------------------

    def create_owl(self, name: str, *, model: str | None = None) -> dict:
        body: dict = {"name": name[:80], "request_id": request_id()}
        if model:
            body["model"] = model
        return self.request("POST", "/v1/bots", body)

    def delete_owl(self, bot_id: str) -> None:
        self.request("DELETE", f"/v1/bots/{quote(bot_id)}")

    def chat(self, bot_id: str, title: str = "Benchmark") -> dict:
        return self.request("POST", f"/v1/bots/{quote(bot_id)}/chats", {"title": title[:160]})

    def conversation(self, chat_id: str) -> dict:
        return self.request("GET", f"/v1/chats/{quote(chat_id)}")

    # Tasks -----------------------------------------------------------------------------

    def send(self, chat_id: str, message: str, file_ids: list[str] | None = None) -> dict:
        body = {"message": message, "request_id": request_id(), "file_ids": file_ids or []}
        return self.request("POST", f"/v1/chats/{quote(chat_id)}/tasks", body)

    def task(self, task_id: str) -> dict:
        return self.request("GET", f"/v1/tasks/{quote(task_id)}")

    def cancel(self, task_id: str) -> dict:
        return self.request("POST", f"/v1/tasks/{quote(task_id)}/cancel")

    # Library ---------------------------------------------------------------------------

    def upload(self, bot_id: str, path: Path) -> dict:
        """Put a file in the owl's Library, whole or in chunks; returns its file record."""
        data = Path(path).read_bytes()
        owl = quote(bot_id)
        if len(data) <= INLINE_FILE_BYTES:
            return self.request(
                "POST",
                f"/v1/bots/{owl}/files/binary",
                {"name": Path(path).name, "data_base64": base64.b64encode(data).decode("ascii")},
            )
        upload = self.request(
            "POST",
            f"/v1/bots/{owl}/uploads",
            {"name": Path(path).name, "size": len(data), "request_id": request_id()},
        )
        for offset in range(0, len(data), FILE_CHUNK_BYTES):
            chunk = data[offset : offset + FILE_CHUNK_BYTES]
            self.request(
                "PUT",
                f"/v1/bots/{owl}/uploads/{quote(upload['id'])}",
                {"offset": offset, "data_base64": base64.b64encode(chunk).decode("ascii")},
            )
        receipt = self.request("POST", f"/v1/bots/{owl}/uploads/{quote(upload['id'])}/complete")
        return self.request("GET", f"/v1/bots/{owl}/files/{quote(receipt['file_id'])}")

    def task_files(self, bot_id: str, task_id: str) -> list[dict]:
        """Every Library file the task saved, every revision, newest first."""
        files: list[dict] = []
        before = None
        while True:
            body: dict = {"source_task_id": task_id, "limit": PAGE_LIMIT}
            if before:
                body["before"] = before
            page = self.request("POST", f"/v1/bots/{quote(bot_id)}/files/page", body)
            files.extend(page.get("items", []))
            before = page.get("next_before")
            if not before:
                return files

    def download(self, bot_id: str, file: dict) -> bytes:
        owl, file_id = quote(bot_id), quote(file["id"])
        size = file.get("size_bytes") or 0
        if size <= INLINE_FILE_BYTES:
            payload = self.request("GET", f"/v1/bots/{owl}/files/{file_id}/download")
            data = base64.b64decode(payload["data_base64"])
        else:
            parts, offset = [], 0
            while offset < size:
                chunk = self.request("GET", f"/v1/bots/{owl}/files/{file_id}/chunks/{offset}")
                piece = base64.b64decode(chunk["data_base64"])
                if not piece:
                    break
                parts.append(piece)
                offset = chunk["next_offset"]
            data = b"".join(parts)
        expected = file.get("sha256")
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise OwlsError(200, "the downloaded bytes do not match the file's sha256", file_id)
        return data
