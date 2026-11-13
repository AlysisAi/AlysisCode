"""A computer on this device keeps its page while a task waits for the person (an approval)."""

from __future__ import annotations

import pytest
from test_bots_browser import Harness, task


def lab_with(retain: bool) -> Harness:
    lab = Harness()

    def hook(executor, cancel):
        # Like the browser of an owl computer: it retires on release; on this device it may
        # stay while its task waits for the person.
        executor.retire_on_release = True
        executor.retain_while_waiting = retain
        executor.finish_calls = 0

        def finish(cancel=None):
            executor.finish_calls += 1
            executor.closed = True

        executor.finish = finish

    lab.factory_hook = hook
    return lab


@pytest.fixture
def kept():
    lab = lab_with(True)
    yield lab
    lab.adapter.close()


def test_a_waiting_task_resumes_on_the_same_page(kept):
    kept.call()
    kept.adapter.release("task-a", cancelled=False, waiting=True)
    assert not kept.executors[0].closed
    kept.now = 5
    observation = kept.call()  # after the approval: the page is still there
    assert observation["source_id"] and len(kept.executors) == 1 and not kept.executors[0].closed


def test_an_unused_kept_computer_finishes_cleanly_at_idle_expiry(kept):
    kept.call()
    kept.adapter.release("task-a", cancelled=False, waiting=True)
    kept.now = 9
    kept.adapter.reap()
    assert not kept.executors[0].closed
    kept.now = 10
    kept.adapter.reap()
    executor = kept.executors[0]
    assert executor.closed and executor.finish_calls == 1 and executor.close_calls == 0


def test_another_task_of_the_owl_gets_its_own_computer(kept):
    kept.call()
    kept.adapter.release("task-a", cancelled=False, waiting=True)
    kept.call(request=task(identifier="task-b"))
    assert len(kept.executors) == 2
    assert kept.executors[0].finish_calls == 1 and not kept.executors[1].closed


@pytest.mark.parametrize("cancelled,finished", [(True, 0), (False, 1)])
def test_the_waiting_task_ending_closes_its_kept_computer(kept, cancelled, finished):
    kept.call()
    kept.adapter.release("task-a", cancelled=False, waiting=True)
    kept.adapter.release("task-a", cancelled=cancelled)
    executor = kept.executors[0]
    assert executor.closed and executor.finish_calls == finished
    assert executor.close_calls == (1 if cancelled else 0)


def test_a_cloud_computer_still_closes_while_its_task_waits():
    lab = lab_with(False)
    try:
        lab.call()
        lab.adapter.release("task-a", cancelled=False, waiting=True)
        assert lab.executors[0].closed and lab.executors[0].finish_calls == 1
    finally:
        lab.adapter.close()


def test_a_finished_task_still_retires_its_computer(kept):
    kept.call()
    kept.adapter.release("task-a", cancelled=False)
    assert kept.executors[0].closed and kept.executors[0].finish_calls == 1
