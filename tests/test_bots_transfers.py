import base64
import hashlib

import pytest

from alysis_code.bots.files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from alysis_code.bots.models import BotInput
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput


def setup(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Files", purpose="Test transfers"))
    return store, bot["id"], FileTransfers(store, "owner")


def chunk(offset, data):
    return UploadChunk(offset=offset, data_base64=base64.b64encode(data).decode())


def test_50mib_stream_restart_retry_and_single_publication(tmp_path):
    store, bot, transfers = setup(tmp_path)
    request = UploadInput(name="large.bin", size=MAX_FILE_BYTES, request_id="large-upload")
    upload = transfers.begin(bot, request)
    assert transfers.begin(bot, request)["id"] == upload["id"]
    assert store.list_files("owner", bot) == []
    block = bytes(range(256)) * (FILE_CHUNK_BYTES // 256)
    transfers.append(bot, upload["id"], chunk(0, block))
    transfers = FileTransfers(Store(store.path), "owner")
    assert transfers.append(bot, upload["id"], chunk(0, block))["received"] == len(block)
    with pytest.raises(Conflict):
        transfers.append(bot, upload["id"], chunk(0, b"different"))
    with pytest.raises(Conflict):
        transfers.complete(bot, upload["id"])
    digest = hashlib.sha256(block)
    for offset in range(len(block), MAX_FILE_BYTES, len(block)):
        transfers.append(bot, upload["id"], chunk(offset, block))
        digest.update(block)
    saved = transfers.complete(bot, upload["id"])
    assert transfers.complete(bot, upload["id"]) == saved
    assert transfers.cancel(bot, upload["id"])["status"] == "complete"
    assert len(store.list_files("owner", bot)) == 1
    result = store.file("owner", bot, saved["file_id"])
    assert result["size_bytes"] == MAX_FILE_BYTES and result["sha256"] == digest.hexdigest()
    actual = hashlib.sha256()
    for data in store.file_chunks("owner", bot, result["id"]):
        assert len(data) <= FILE_CHUNK_BYTES
        actual.update(data)
    assert actual.hexdigest() == digest.hexdigest()
    with pytest.raises(LimitReached, match="chunked"):
        store.file_data("owner", bot, result["id"])


def test_partial_uploads_reserve_quota_and_cancel_releases_it(tmp_path):
    store, bot, transfers = setup(tmp_path)
    store.set_library_limit("owner", 2 * MAX_FILE_BYTES)
    first = transfers.begin(
        bot, UploadInput(name="first", size=MAX_FILE_BYTES, request_id="first-upload")
    )
    transfers.begin(
        bot, UploadInput(name="second", size=MAX_FILE_BYTES, request_id="second-upload")
    )
    with pytest.raises(LimitReached):
        store.save_bytes("owner", bot, "overflow", b"x")
    with pytest.raises(Conflict):
        store.save_bytes("owner", bot, "first", b"x")
    assert transfers.cancel(bot, first["id"])["status"] == "cancelled"
    with pytest.raises(Conflict):
        transfers.append(bot, first["id"], chunk(0, b"x"))
    store.save_bytes("owner", bot, "released", b"x")
    assert [f["name"] for f in store.list_files("owner", bot)] == ["released"]
    with store.connect() as db:
        assert (
            db.execute("SELECT data FROM file_uploads WHERE id=?", (first["id"],)).fetchone()[0]
            is None
        )


def test_stream_failure_cannot_publish_and_cross_owner_cannot_read(tmp_path):
    store, bot, transfers = setup(tmp_path)

    def interrupted():
        yield b"partial"
        raise RuntimeError("Interrupted")

    with pytest.raises(RuntimeError):
        store.save_stream("owner", bot, "partial", interrupted(), 100)
    assert store.list_files("owner", bot) == []
    upload = transfers.begin(bot, UploadInput(name="empty", size=0, request_id="empty-upload"))
    with pytest.raises(NotFound):
        FileTransfers(store, "another").read(bot, upload["id"])
    saved = transfers.complete(bot, upload["id"])
    assert store.file("owner", bot, saved["file_id"])["size_bytes"] == 0
    assert transfers.chunk(bot, saved["file_id"], 0)["next_offset"] == 0
