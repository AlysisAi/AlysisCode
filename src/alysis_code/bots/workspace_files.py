"""Trusted streaming bridge to an executor's explicit backing workspace.

No model paths, host file discovery or payloads in the desktop command pipe.
Staging lives beside (never inside) the mounted workspace and publishes without
overwrite only after complete byte/digest verification.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from .desktop_linux import DesktopError
from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from .models import FileInput


def stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def import_file(store, owner, task, source, workspace: Path, token):
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with tempfile.TemporaryDirectory(prefix=".alysis-transfer-", dir=workspace.parent) as temp:
            path = Path(temp) / "payload"
            size, digest = 0, hashlib.sha256()
            with path.open("xb") as target:
                with closing(store.file_chunks(owner, task["bot_id"], source["id"])) as chunks:
                    for data in chunks:
                        token.throw_if_cancelled()
                        size += len(data)
                        if size > MAX_FILE_BYTES:
                            raise DesktopError("File exceeds the 50 MiB transfer allowance.")
                        target.write(data)
                        digest.update(data)
                target.flush()
                os.fsync(target.fileno())
            if size != source["size_bytes"] or digest.hexdigest() != source["sha256"]:
                raise DesktopError("The source changed during transfer; no file was imported.")
            token.throw_if_cancelled()
            store.file(owner, task["bot_id"], source["id"])
            if not store.bot(owner, task["bot_id"])["files_enabled"]:
                raise DesktopError("File transfer permission was removed.")
            os.link(path, source["name"], dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
            return {
                "name": source["name"],
                "bytes": size,
                "sha256": digest.hexdigest(),
                "written": True,
                "source_file_id": source["id"],
            }
    except OSError:
        raise DesktopError(
            "Import failed. Use a new file name and inspect the workspace before retrying."
        ) from None
    finally:
        os.close(directory)


def export_file(
    store, owner, task, name, workspace: Path, token, *, previous_file_id=None, expected_sha256=None
):
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise DesktopError("Export requires a regular file of at most 50 MiB.")

        def chunks():
            digest = hashlib.sha256()
            while data := os.read(fd, FILE_CHUNK_BYTES):
                token.throw_if_cancelled()
                if stamp(os.fstat(fd)) != stamp(before):
                    raise DesktopError(
                        "File changed during export. Stop its writer before retrying."
                    )
                digest.update(data)
                yield data
            token.throw_if_cancelled()
            if stamp(os.fstat(fd)) != stamp(before):
                raise DesktopError("File changed during export; nothing was saved.")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise DesktopError(
                    "The file changed after validation; check it again before saving."
                )

        if before.st_size <= 100000:
            data = b"".join(chunks())
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                content = None
            if content is not None and "\0" not in content:
                return store.save_file(
                    owner,
                    task["bot_id"],
                    FileInput(name=name, content=content, previous_file_id=previous_file_id),
                    source_task_id=task["id"],
                )
            os.lseek(fd, 0, os.SEEK_SET)
        return store.save_stream(
            owner,
            task["bot_id"],
            name,
            chunks(),
            before.st_size,
            source_task_id=task["id"],
            previous_file_id=previous_file_id,
        )
    except FileNotFoundError:
        raise DesktopError(
            "The source file does not exist in /workspace. The name argument selects the "
            "actual workspace file; it does not rename a library copy. Inspect the workspace "
            "and create or copy the file under the desired unused name before exporting."
        ) from None
    except OSError:
        raise DesktopError(
            "Export requires an existing regular workspace file without symlinks."
        ) from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)
