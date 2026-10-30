"""Job membership observation contracts with no Windows calls or native workload."""

import pytest

from alysis_code._windows_sandbox_job import WindowsSandboxJob


class MembershipKernel:
    def __init__(self, *, never_complete=False):
        self.never_complete = never_complete
        self.capacities = []
        self.opened = []

    def QueryInformationJobObject(self, job, information_class, pointer, size, returned):
        assert job == 123 and information_class == 3
        info = pointer._obj
        self.capacities.append(len(info.ids))
        # The API reports success, but its count fields still describe an
        # incomplete snapshot. No handle may be accepted from that response.
        info.assigned = 65537 if self.never_complete else 40
        ids = [999] if self.never_complete or len(self.capacities) == 1 else range(40)
        info.count = len(ids)
        for index, pid in enumerate(ids):
            info.ids[index] = pid
        return True

    def OpenProcess(self, access, inherit, pid):
        assert access == 0x1000 | 0x100000 and inherit is False
        self.opened.append(pid)
        return pid + 1000

    def IsProcessInJob(self, process, job, member):
        assert job == 123 and process in range(1000, 1040)
        member._obj.value = True
        return True


def observed_job(kernel):
    job = WindowsSandboxJob.__new__(WindowsSandboxJob)
    job._kernel = kernel
    job._handle = 123
    return job


def test_successful_incomplete_membership_query_retries_before_opening_handles():
    kernel = MembershipKernel()
    handles = observed_job(kernel)._original_members()
    assert kernel.capacities == [32, 64]
    assert kernel.opened == list(range(40))
    assert handles == list(range(1000, 1040))


def test_successful_membership_queries_that_never_complete_fail_closed():
    kernel = MembershipKernel(never_complete=True)
    with pytest.raises(
        RuntimeError, match="snapshot is incomplete; cancellation is not acknowledged"
    ):
        observed_job(kernel)._original_members()
    assert kernel.capacities == [32, 65536]
    assert kernel.opened == []
