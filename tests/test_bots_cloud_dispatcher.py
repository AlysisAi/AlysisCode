"""The Cloud computer's dispatcher (cloud_desktop_worker.serve) outlives a bridge client that goes
away: one whose wait ran out, or whose command its provider stopped, before the answer was sent.
Its browser and desktop stay for the next request. A replayed request still ends it."""

import json
import shutil
import socket
import threading
import time
from uuid import uuid4

import pytest

from alysis_code.bots import cloud_desktop_worker as worker


@pytest.fixture
def dispatcher(monkeypatch):
    """serve() in a thread, with each request held until the test lets it be read."""
    session = uuid4().hex
    release = threading.Event()
    real_receive = worker.receive

    def held_receive(connection):
        assert release.wait(10)
        return real_receive(connection)

    monkeypatch.setattr(worker, "receive", held_receive)
    ended = threading.Event()
    failure = []

    def run():
        try:
            worker.serve(session, memory_sleep=False)
        except BaseException as error:  # what ended it, for the assertions
            failure.append(error)
        finally:
            ended.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    root = worker.root_for(session)
    deadline = time.monotonic() + 10
    while not (root / "control.sock").exists():
        assert time.monotonic() < deadline and not ended.is_set(), failure
        time.sleep(0.01)
    yield session, root, release, ended, failure
    shutil.rmtree(root, ignore_errors=True)


def connect(root):
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(10)
    client.connect(str(root / "control.sock"))
    return client


def ping(session, identity=None):
    request = {"id": identity or uuid4().hex, "session": session, "operation": "ping"}
    return (json.dumps({**request, "arguments": {}}) + "\n").encode()


def ask(root, payload):
    """Send one request and read its whole answer (worker.receive is held in these tests)."""
    with connect(root) as client:
        client.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            assert chunk, "the dispatcher closed without an answer"
            data += chunk
        return json.loads(data)


def test_a_client_gone_before_its_answer_does_not_end_the_dispatcher(dispatcher):
    session, root, release, ended, failure = dispatcher
    # The client sends its request and goes away before the dispatcher answers.
    client = connect(root)
    client.sendall(ping(session))
    client.close()
    release.set()
    time.sleep(0.3)
    assert not ended.is_set(), failure
    # The next request is answered: everything on the computer stayed.
    reply = ask(root, ping(session))
    assert reply["session"] == session and reply["result"]["ready"] is True


def test_a_client_gone_before_its_request_arrived_whole_does_not_end_the_dispatcher(dispatcher):
    session, root, release, ended, failure = dispatcher
    client = connect(root)
    client.sendall(ping(session)[:20])  # part of a request, then gone
    client.close()
    release.set()
    time.sleep(0.3)
    assert not ended.is_set(), failure
    assert ask(root, ping(session))["result"]["ready"] is True


def test_a_replayed_request_still_ends_the_dispatcher(dispatcher):
    session, root, release, ended, failure = dispatcher
    release.set()
    identity = uuid4().hex
    assert ask(root, ping(session, identity))["result"]["ready"] is True
    with connect(root) as client:
        client.sendall(ping(session, identity))
        assert ended.wait(10)
    assert isinstance(failure[0], ValueError)
