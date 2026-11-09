//! Cooperative cancellation for the trusted legacy broker, never the workload.
//! Event handles arrive over the engine's private request channel and are not
//! inherited. An emergency exit intentionally cannot acknowledge cleanup.

use anyhow::{Context, Result, bail, ensure};
use std::cell::RefCell;
use windows::Win32::Foundation::{HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows::Win32::System::Threading::{
    INFINITE, SetEvent, WaitForMultipleObjects, WaitForSingleObject,
};

thread_local! {
    static EVENTS: RefCell<Option<(crate::util::OwnedHandle, crate::util::OwnedHandle)>> = const { RefCell::new(None) };
}

/// The trusted stdin request supplies handles duplicated into this process.
/// Neither is borrowed from another native subsystem or passed to a child.
pub unsafe fn install(cancel: usize, complete: usize) -> Result<()> {
    ensure!(cancel > 0 && complete > 0 && cancel != complete, "invalid lifecycle handles");
    EVENTS.with_borrow_mut(|events| {
        ensure!(events.is_none(), "lifecycle already installed");
        *events = Some((
            crate::util::OwnedHandle(HANDLE(cancel as *mut _)),
            crate::util::OwnedHandle(HANDLE(complete as *mut _)),
        ));
        Ok(())
    })
}

pub fn check() -> Result<()> {
    EVENTS.with_borrow(|events| {
        if let Some((cancel, _)) = events {
            let status = unsafe { WaitForSingleObject(cancel.raw(), 0) };
            ensure!(status == WAIT_TIMEOUT, "native execution cancelled or cancellation handle invalid");
        }
        Ok(())
    })
}

pub fn wait(process: HANDLE) -> Result<()> {
    EVENTS.with_borrow(|events| {
        let status = if let Some((cancel, _)) = events {
            // Cancellation wins if both become signaled together.
            unsafe { WaitForMultipleObjects(&[cancel.raw(), process], false, INFINITE) }
        } else {
            unsafe { WaitForSingleObject(process, INFINITE) }
        };
        let expected = if events.is_some() { WAIT_OBJECT_0.0 + 1 } else { WAIT_OBJECT_0.0 };
        if status.0 != expected {
            bail!("native execution cancelled or process wait failed: {:#x}", status.0);
        }
        Ok(())
    })
}

/// Call only after the policy function returns and every process guard drops.
pub fn acknowledge() -> Result<()> {
    EVENTS.with_borrow(|events| {
        if let Some((_, complete)) = events {
            unsafe { SetEvent(complete.raw()) }.context("acknowledge native cleanup")?;
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::Foundation::{DuplicateHandle, DUPLICATE_SAME_ACCESS};
    use windows::Win32::System::Threading::{CreateEventW, GetCurrentProcess};

    fn duplicate(handle: HANDLE) -> usize {
        let mut copied = HANDLE::default();
        unsafe {
            DuplicateHandle(GetCurrentProcess(), handle, GetCurrentProcess(),
                &mut copied, 0, false, DUPLICATE_SAME_ACCESS).unwrap();
        }
        copied.0 as usize
    }

    #[test]
    fn cancellation_does_not_acknowledge_until_cleanup_returns() {
        std::thread::spawn(|| {
            let cancel = crate::util::OwnedHandle(unsafe { CreateEventW(None, true, false, None).unwrap() });
            let complete = crate::util::OwnedHandle(unsafe { CreateEventW(None, true, false, None).unwrap() });
            unsafe { install(duplicate(cancel.raw()), duplicate(complete.raw())).unwrap() };
            check().unwrap();
            unsafe { SetEvent(cancel.raw()).unwrap() };
            assert!(check().is_err());
            assert!(wait(unsafe { GetCurrentProcess() }).is_err());
            assert_eq!(unsafe { WaitForSingleObject(complete.raw(), 0) }, WAIT_TIMEOUT);
            acknowledge().unwrap();
            assert_eq!(unsafe { WaitForSingleObject(complete.raw(), 0) }, WAIT_OBJECT_0);
        }).join().unwrap();
    }
}
