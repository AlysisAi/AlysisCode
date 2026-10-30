//! Account policy is shared: execution, recovery and maintenance must serialize.
use anyhow::{Result, ensure};
use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Threading::{CreateMutexW, ReleaseMutex, WaitForSingleObject};
use windows::core::PCWSTR;

pub struct ExecutionLease(HANDLE);

impl ExecutionLease {
    pub fn acquire() -> Result<Self> {
        let name = crate::util::wstr("Global\\AlysisSandboxExecutionV1");
        let handle = unsafe { CreateMutexW(None, false, PCWSTR(name.as_ptr()))? };
        let lease = Self(handle);
        // Never wait behind a server or allow maintenance to dismantle its policy.
        ensure!(
            unsafe { WaitForSingleObject(handle, 0) } == WAIT_OBJECT_0,
            "sandbox identity is busy or its previous owner crashed; stop other sandbox commands and run sandbox doctor"
        );
        Ok(lease)
    }

    /// Called only by the process that will perform maintenance (after UAC).
    /// Holding the same lease as execution closes the check/mutation race.
    pub fn maintenance() -> Result<Self> {
        let lease = Self::acquire()?;
        crate::local_account_host::require_local_sam()?;
        if let Some(setup) = crate::install::read_setup()? {
            ensure!(
                setup.owner_user_sid == crate::sid::current_user_sid()?,
                "sandbox belongs to another Windows user; use consent elevation as its owner"
            );
        }
        let journal = crate::state_db::session_store_dir()?.join("execution-pending.json");
        ensure!(
            !journal.try_exists()?,
            "unfinished native sandbox execution; run alysis sandbox recover-native before setup or removal"
        );
        Ok(lease)
    }
}

impl Drop for ExecutionLease {
    fn drop(&mut self) {
        unsafe {
            let _ = ReleaseMutex(self.0);
            let _ = CloseHandle(self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn execution_and_maintenance_cannot_overlap() {
        let lease = ExecutionLease::acquire().unwrap();
        // Windows mutexes are recursive on one thread, so use another thread.
        assert!(
            std::thread::spawn(|| ExecutionLease::acquire().is_err())
                .join()
                .unwrap()
        );
        drop(lease);
        assert!(
            std::thread::spawn(|| ExecutionLease::acquire().is_ok())
                .join()
                .unwrap()
        );
    }
}
