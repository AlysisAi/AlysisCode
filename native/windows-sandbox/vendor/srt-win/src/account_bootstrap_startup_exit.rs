//! Exit proof for the bounded, trusted startup group only. Later admitted
//! account children and arbitrary descendants need their own retained proof.
//! Job accounting, an exit code, or a PID never substitutes for a signaled
//! original process object. This type is neither a resource retirement receipt
//! nor a cold-recovery capability.
use super::*;
use windows::Win32::System::JobObjects::*;

#[derive(Default)]
pub(super) struct StartupExit {
    originals: Vec<OwnedHandle>,
    attempted: bool,
    complete: bool,
    later_work_possible: bool,
}

#[cfg(test)]
thread_local! {
    static BEFORE_TERMINATE: std::cell::RefCell<Option<Box<dyn FnOnce(&Job)>>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
mod tests {
    use super::*;

    // A fixed OS program is born atomically in the job and never resumed.
    fn suspended(job: &Job) -> SpawnedChild {
        let image = std::path::PathBuf::from(os_directory(false).unwrap()).join("cmd.exe");
        let app = wstr(image.to_str().unwrap());
        let mut command = wstr(&format!(
            "{} /d /c exit 0",
            crate::launch::quote_arg(image.to_str().unwrap())
        ));
        let mut attrs = ProcThreadAttrs::new(1).unwrap();
        attrs.set_job(job).unwrap();
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.lpAttributeList = attrs.list();
        let mut pi = PROCESS_INFORMATION::default();
        unsafe {
            CreateProcessW(
                pcwstr(&app),
                Some(windows::core::PWSTR(command.as_mut_ptr())),
                None,
                None,
                false,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                None,
                None,
                &startup.StartupInfo,
                &mut pi,
            )
            .unwrap();
        }
        SpawnedChild::new(pi)
    }

    #[test]
    fn startup_inventory_deduplicates_and_waits_original_root() {
        let job = Job::new(false).unwrap();
        let child = suspended(&job);
        let mut inventory = StartupExit::default();
        inventory.retain_root(&job, child.process()).unwrap();
        inventory.capture(&job).unwrap();
        assert_eq!(inventory.originals.len(), 1);
        assert!(
            unsafe { CompareObjectHandles(inventory.originals[0].raw(), child.process()) }
                .as_bool()
        );
        inventory.drain(&job).unwrap();
        assert_eq!(
            unsafe { WaitForSingleObject(child.process(), 0) },
            WAIT_OBJECT_0
        );
    }

    #[test]
    fn departed_unobserved_startup_member_cannot_be_erased_by_empty_accounting() {
        let job = Job::new(false).unwrap();
        let child = suspended(&job);
        let mut inventory = StartupExit::default();
        inventory.retain_root(&job, child.process()).unwrap();
        let unknown = suspended(&job);
        drop(unknown); // TotalProcesses remembers a birth absent from our references.
        assert!(inventory.capture(&job).is_err());
        assert!(inventory.capture(&job).is_err());
        assert!(inventory.drain(&job).is_err());
        assert_eq!(
            unsafe { WaitForSingleObject(child.process(), 0) },
            WAIT_OBJECT_0
        );
        assert!(!inventory.complete);
    }

    #[test]
    fn foreign_root_is_refused_and_failed_capture_still_terminates_only_own_job() {
        let job = Job::new(false).unwrap();
        let other = Job::new(false).unwrap();
        let child = suspended(&job);
        let foreign = suspended(&other);
        let mut inventory = StartupExit::default();
        assert!(inventory.retain_root(&job, foreign.process()).is_err());
        assert!(inventory.drain(&job).is_err());
        // Failed capture returns no exit proof. This test's separate observer
        // only checks that termination was requested despite that failure.
        assert_eq!(
            unsafe { WaitForSingleObject(child.process(), 5000) },
            WAIT_OBJECT_0
        );
        assert_eq!(
            unsafe { WaitForSingleObject(foreign.process(), 0) },
            WAIT_TIMEOUT
        );
    }

    #[test]
    fn failed_drain_cannot_be_retried_into_success_after_late_startup_birth() {
        for captured_before_failed_ready in [false, true] {
            let job = Job::new(false).unwrap();
            let child = suspended(&job);
            let mut inventory = StartupExit::default();
            inventory.retain_root(&job, child.process()).unwrap();
            if captured_before_failed_ready {
                inventory.capture(&job).unwrap();
            }
            BEFORE_TERMINATE.with(|s| *s.borrow_mut() = Some(Box::new(|job| drop(suspended(job)))));
            assert!(inventory.drain(&job).is_err());
            assert_eq!(
                unsafe { WaitForSingleObject(child.process(), 0) },
                WAIT_OBJECT_0
            );
            assert!(inventory.drain(&job).is_err());
            assert!(!inventory.complete);
        }
    }
}

fn counts(job: &Job) -> Result<(u32, u32)> {
    let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    let mut returned = 0;
    unsafe {
        QueryInformationJobObject(
            Some(job.raw()),
            JobObjectBasicAccountingInformation,
            (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            Some(&mut returned),
        )?;
    }
    ensure!(
        returned as usize == size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>(),
        "incomplete startup accounting"
    );
    Ok((info.ActiveProcesses, info.TotalProcesses))
}

impl StartupExit {
    // Set before attempting the authenticated ACK: a failed send may have
    // delivered it. Capture alone must not authorize later-member accounting.
    pub(super) fn allow_later_work(&mut self, job: &Job) -> Result<()> {
        ensure!(
            self.complete
                && !self.later_work_possible
                && counts(job)?.1 as usize == self.originals.len(),
            "startup inventory changed before later-work acknowledgment"
        );
        self.later_work_possible = true;
        Ok(())
    }
    /// Called before any fallible birth observation and before initial resume.
    pub(super) fn retain_root(&mut self, job: &Job, original: HANDLE) -> Result<()> {
        ensure!(
            self.originals.is_empty() && !self.attempted,
            "startup root already retained"
        );
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                original,
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let retained = OwnedHandle(raw);
        let mut member = windows::core::BOOL::default();
        unsafe {
            IsProcessInJob(raw, Some(job.raw()), &mut member)?;
        }
        ensure!(
            member.as_bool() && unsafe { CompareObjectHandles(original, raw) }.as_bool(),
            "startup original differs from its job/root"
        );
        self.originals.push(retained);
        Ok(())
    }

    /// While the trusted entry waits for its first command, retain every
    /// startup member. A hidden console host can be born after initial resume.
    /// No later child may be requested until this snapshot is complete.
    pub(super) fn capture(&mut self, job: &Job) -> Result<()> {
        self.complete = false;
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "startup capture already attempted"
        );
        ensure!(self.originals.len() == 1, "original startup root missing");
        #[repr(C)]
        #[derive(Default)]
        struct List {
            assigned: u32,
            listed: u32,
            ids: [usize; 2],
        }
        let before = counts(job)?;
        ensure!(
            (1..=2).contains(&before.1),
            "unexpected startup process history"
        );
        let mut list = List::default();
        let mut returned = 0;
        unsafe {
            QueryInformationJobObject(
                Some(job.raw()),
                JobObjectBasicProcessIdList,
                (&mut list as *mut List).cast(),
                size_of::<List>() as u32,
                Some(&mut returned),
            )?;
        }
        ensure!(
            list.listed <= 2
                && list.assigned == list.listed
                && returned as usize
                    >= std::mem::offset_of!(List, ids) + list.listed as usize * size_of::<usize>()
                && returned as usize <= size_of::<List>(),
            "incomplete startup process list"
        );
        let conhost = std::path::PathBuf::from(os_directory(false)?).join("conhost.exe");
        for &pid in &list.ids[..list.listed as usize] {
            let process = OwnedHandle(unsafe {
                OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                    false,
                    u32::try_from(pid)?,
                )?
            });
            let mut member = windows::core::BOOL::default();
            unsafe {
                IsProcessInJob(process.raw(), Some(job.raw()), &mut member)?;
            }
            ensure!(
                member.as_bool(),
                "startup PID no longer belongs to original job"
            );
            if self
                .originals
                .iter()
                .any(|p| unsafe { CompareObjectHandles(p.raw(), process.raw()).as_bool() })
            {
                continue;
            }
            let mut image = vec![0u16; 32768];
            let mut length = image.len() as u32;
            unsafe {
                QueryFullProcessImageNameW(
                    process.raw(),
                    PROCESS_NAME_WIN32,
                    windows::core::PWSTR(image.as_mut_ptr()),
                    &mut length,
                )?;
            }
            ensure!(
                String::from_utf16(&image[..length as usize])?.eq_ignore_ascii_case(
                    conhost
                        .to_str()
                        .context("system console image is not Unicode")?
                ),
                "unexpected startup member image"
            );
            // The image check limits this inventory's scope; it confers no
            // credentials or execution authority on the discovered process.
            self.originals.push(process);
        }
        let after = counts(job)?;
        ensure!(
            before.1 == after.1 && after.1 as usize == self.originals.len(),
            "startup member departed or appeared without a retained reference"
        );
        self.complete = true;
        Ok(())
    }

    /// Request termination even when capture failed. A partial inventory must
    /// stop outer cleanup; another attempt cannot erase the uncertainty.
    pub(super) fn drain(&mut self, job: &Job) -> Result<()> {
        let result = self.drain_inner(job);
        if result.is_err() {
            self.complete = false;
        }
        result
    }

    fn drain_inner(&mut self, job: &Job) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(30);
        let pre_ready = !self.later_work_possible;
        let capture = if !self.attempted {
            self.capture(job)
        } else {
            if self.complete {
                Ok(())
            } else {
                Err(anyhow::anyhow!("startup capture outcome is uncertain"))
            }
        };
        #[cfg(test)]
        BEFORE_TERMINATE.with(|s| {
            if let Some(callback) = s.borrow_mut().take() {
                callback(job);
            }
        });
        let termination = job.terminate_and_wait();
        for original in &self.originals {
            let remaining = deadline
                .saturating_duration_since(Instant::now())
                .as_millis() as u32;
            ensure!(
                unsafe { WaitForSingleObject(original.raw(), remaining) } == WAIT_OBJECT_0,
                "original startup process exit is unverified"
            );
        }
        termination?;
        capture?;
        ensure!(self.complete, "startup exit inventory is incomplete");
        if pre_ready {
            ensure!(
                counts(job)?.1 as usize == self.originals.len(),
                "startup process appeared during failed-start termination"
            );
        }
        Ok(())
    }
}
