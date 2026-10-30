//! Original-handle birth of a trusted host bootstrap. The current
//! image is pinned before CreateProcessW, and JOB_LIST assigns the strict job
//! atomically. No account logon, credentials, profile or shared-ACL effect here.
//! Resume requires a live committed ticket. Account logon/commands stay closed.
use super::*;
use crate::{
    account_state::image::PinnedRunnerImage,
    job::Job,
    launch::{ProcThreadAttrs, SpawnedChild},
};

#[path = "account_bootstrap_startup_exit.rs"]
mod startup_exit;
pub(crate) const STARTUP_CAPTURED: &[u8] = b"alysis-bootstrap-startup-captured-v1";

pub(crate) struct SuspendedBootstrap<'a> {
    child: SpawnedChild,
    image: &'a PinnedRunnerImage,
    job: &'a Job,
    offer_attempted: bool,
    resumed: bool,
    startup_exit: Option<startup_exit::StartupExit>,
}

impl Drop for SuspendedBootstrap<'_> {
    fn drop(&mut self) {
        // This runs before SpawnedChild can initiate exit and remove the
        // hidden console host from the job's active-process enumeration.
        if let Some(startup) = &mut self.startup_exit {
            if let Err(error) = startup.drain(self.job) {
                crate::util::exit_for_unverified_cleanup("bootstrap startup", &error);
            }
        }
    }
}

#[derive(Clone, Copy)]
enum Entry {
    Bootstrap,
    #[cfg(test)]
    PairedFamilyFixture,
    #[cfg(test)]
    ControlFixture,
    #[cfg(test)]
    WaitingFixture,
    #[cfg(test)]
    OfferFixture,
}

impl<'a> SuspendedBootstrap<'a> {
    pub(crate) fn allow_post_startup_work(&mut self) -> Result<()> {
        self.startup_exit
            .as_mut()
            .context("startup inventory unavailable")?
            .allow_later_work(self.job)
    }
    pub(crate) fn capture_startup_exit(&mut self) -> Result<()> {
        self.startup_exit
            .as_mut()
            .context("startup inventory unavailable")?
            .capture(self.job)
    }
    /// Bounded fixture fault against this retained original process only.
    /// The paired coordinator still owes both-job draining and pending evidence.
    #[cfg(test)]
    pub(crate) fn fixture_terminate_original_source(&self) -> Result<()> {
        self.durable_observation()?;
        unsafe {
            TerminateProcess(self.child.process(), 74)?;
        }
        ensure!(
            unsafe { WaitForSingleObject(self.child.process(), 5000) } == WAIT_OBJECT_0,
            "fixed original source did not stop"
        );
        Ok(())
    }
    /// Only a live, single-use write-ahead ticket can reach this adapter. The
    /// result remains suspended until the separate resume ticket is committed.
    pub(crate) fn prepare_recorded(
        intent: &mut crate::account_state::execution::bootstrap_plan::lifecycle::BirthIntent<
            'a,
            '_,
            '_,
            '_,
        >,
    ) -> Result<Self> {
        let (image, job, address) = intent.consume()?;
        Self::create(image, job, address, Entry::Bootstrap)
    }

    /// Paired roles have their own immutable plan and single-use native ticket.
    /// Reuse only the atomic JOB_LIST adapter, not the single-job authority.
    pub(crate) fn prepare_paired_recorded(
        intent: &mut crate::account_state::execution::paired_plan::native::BirthIntent<
            'a,
            '_,
            '_,
            '_,
        >,
    ) -> Result<Self> {
        let (image, job, address) = intent.consume()?;
        Self::create(image, job, address, Entry::Bootstrap)
    }

    #[cfg(test)]
    pub(crate) fn prepare_paired_family_fixture(
        intent: &mut crate::account_state::execution::paired_plan::native::BirthIntent<
            'a,
            '_,
            '_,
            '_,
        >,
    ) -> Result<Self> {
        let (image, job, address) = intent.consume()?;
        Self::create(image, job, address, Entry::PairedFamilyFixture)
    }

    /// Test-only pull from the retained original bootstrap, never a PID reopen.
    #[cfg(test)]
    pub(crate) fn fixture_pull_family_reference(
        &self,
        reference: (u64, u32, u64),
    ) -> Result<OwnedHandle> {
        let (source, pid, created) = reference;
        ensure!(
            source > 0 && source < isize::MAX as u64,
            "invalid family source reference"
        );
        self.peer()?.alive()?;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                self.child.process(),
                HANDLE(source as usize as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let observed = OwnedHandle(raw);
        noninheritable(raw)?;
        let peer = ExpectedPeer::retain(raw)?
            .with_job(self.job)?
            .with_image(self.image)?;
        ensure!(
            peer.pid == pid
                && pid != unsafe { GetProcessId(self.child.process()) }
                && pull::creation_time(raw)? == created
                && created >= pull::creation_time(self.child.process())?,
            "family original process identity differs"
        );
        peer.alive()?;
        Ok(observed)
    }

    pub(crate) fn durable_observation(&self) -> Result<(u32, u64, [u8; 24])> {
        let peer = self.peer()?;
        let mut times = [FILETIME::default(); 4];
        unsafe {
            GetProcessTimes(
                self.child.process(),
                &mut times[0] as *mut _,
                &mut times[1] as *mut _,
                &mut times[2] as *mut _,
                &mut times[3] as *mut _,
            )?;
        }
        let created =
            (u64::from(times[0].dwHighDateTime) << 32) | u64::from(times[0].dwLowDateTime);
        ensure!(created != 0, "bootstrap creation time missing");
        peer.alive()?;
        Ok((peer.pid, created, self.image.file_identity()?))
    }

    #[cfg(test)]
    pub(crate) fn fixture_readonly_observer(&self) -> Result<OwnedHandle> {
        self.original_exit_reference()
    }

    /// Original query/synchronize reference for exit proof. Never a job or
    /// child-mutation handle, and never reconstructed from a PID or message.
    pub(crate) fn original_exit_reference(&self) -> Result<OwnedHandle> {
        Ok(self.peer()?.process)
    }

    /// This gate deliberately precedes every native effect. The existing
    /// empty-job receipt and old separate-job inventory cannot authorize this
    /// different host bootstrap. The recorded route uses a separate live ticket.
    pub(crate) fn prepare(
        image: &'a PinnedRunnerImage,
        job: &'a Job,
        address: &ControlAddress,
    ) -> Result<Self> {
        crate::account_launch::require_lifecycle_authority()?;
        Self::create(image, job, address, Entry::Bootstrap)
    }

    fn create(
        image: &'a PinnedRunnerImage,
        job: &'a Job,
        address: &ControlAddress,
        entry: Entry,
    ) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        containment::prepare(job)?;
        let path = image.application()?;
        let app = wstr(path.to_str().context("bootstrap image is not Unicode")?);
        let mut original_parent = HANDLE::default();
        // Authentication and exit observation only. The broker pulls a child
        // observation using its own original bootstrap handle; the bootstrap
        // never needs DUP_HANDLE access to the broker. No job reference leaves
        // the broker, so last-handle closure still kills every subordinate.
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                GetCurrentProcess(),
                GetCurrentProcess(),
                &mut original_parent,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                true,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let parent = OwnedHandle(original_parent);
        let directory = os_directory(false)?;
        let windows = os_directory(true)?;
        let (args, parent_key, address_key): (String, &str, &str) = match entry {
            #[cfg(test)]
            Entry::Bootstrap | Entry::PairedFamilyFixture => ("--exact account_control::bootstrap::entry::tests::atomic_bootstrap_entry_fixture --ignored --nocapture --test-threads=1".into(),
                "ALYSIS_BOOTSTRAP_ORIGINAL_PEER", "ALYSIS_BOOTSTRAP_ADDRESS"),
            #[cfg(not(test))]
            Entry::Bootstrap => (format!("{} candidate-bootstrap",crate::cli::SRT_WIN_DISPATCH_ARG1),
                "ALYSIS_BOOTSTRAP_ORIGINAL_PEER", "ALYSIS_BOOTSTRAP_ADDRESS"),
            #[cfg(test)]
            Entry::ControlFixture | Entry::WaitingFixture => (
                "--exact account_control::tests::account_control_peer_fixture --ignored --nocapture --test-threads=1".into(),
                "ALYSIS_TEST_CONTROL_ORIGINAL_PEER", "ALYSIS_TEST_CONTROL_ADDRESS"),
            #[cfg(test)]
            Entry::OfferFixture => (
                "--exact account_control::bootstrap::pull::tests::bootstrap_offer_fixture --ignored --nocapture --test-threads=1".into(),
                "ALYSIS_TEST_CONTROL_ORIGINAL_PEER", "ALYSIS_TEST_CONTROL_ADDRESS"),
        };
        let mut command = wstr(&format!(
            "{} {args}",
            crate::launch::quote_arg(path.to_str().unwrap())
        ));
        let values = [
            ("SystemRoot".into(), windows.clone()),
            ("WINDIR".into(), windows),
            ("PATH".into(), directory.clone()),
            (parent_key.into(), (parent.raw().0 as usize).to_string()),
            (address_key.into(), serde_json::to_string(address)?),
        ];
        #[cfg(test)]
        let values = {
            let mut values = values.to_vec();
            if matches!(entry, Entry::PairedFamilyFixture) {
                values.push(("ALYSIS_TEST_PAIRED_FAMILY".into(), "v1".into()));
            }
            if matches!(entry, Entry::Bootstrap) && image.fixture_requires_full_ancestry() {
                values.push(("ALYSIS_TEST_BOOTSTRAP_INSTALLED_IMAGE".into(), "1".into()));
            }
            if matches!(entry, Entry::WaitingFixture) {
                values.push(("ALYSIS_TEST_CONTROL_WAIT_FOR_KILL".into(), "1".into()));
            }
            values
        };
        let mut environment = crate::launch::build_env_block_with_inheritance(&values, false);
        let cwd = wstr(&directory);
        let mut attributes = ProcThreadAttrs::new(2)?;
        attributes.set_handle_list(&[parent.raw()])?;
        attributes.set_job(job)?;
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.lpAttributeList = attributes.list();
        // Repeat immediately before the native birth. None of these checks
        // reopen an image by name or transfer ownership of a job reference.
        containment::prepare(job)?;
        image.application()?;
        let mut info = PROCESS_INFORMATION::default();
        unsafe {
            CreateProcessW(
                pcwstr(&app),
                Some(windows::core::PWSTR(command.as_mut_ptr())),
                None,
                None,
                true,
                CREATE_SUSPENDED
                    | CREATE_NO_WINDOW
                    | CREATE_UNICODE_ENVIRONMENT
                    | EXTENDED_STARTUPINFO_PRESENT,
                Some(environment.as_mut_ptr().cast()),
                pcwstr(&cwd),
                &startup.StartupInfo,
                &mut info,
            )
        }
        .context("create atomically contained host bootstrap")?;
        let child = SpawnedChild::new(info); // Armed before any fallible postcheck.
        let mut born = Self {
            child,
            image,
            job,
            offer_attempted: false,
            resumed: false,
            startup_exit: match entry {
                Entry::Bootstrap => Some(startup_exit::StartupExit::default()),
                #[cfg(test)]
                Entry::PairedFamilyFixture => Some(startup_exit::StartupExit::default()),
                #[cfg(test)]
                _ => None,
            },
        };
        if let Some(startup) = &mut born.startup_exit {
            startup.retain_root(job, born.child.process())?;
        }
        born.peer()?;
        Ok(born)
    }

    /// Native observation from our own original creation result. A pipe frame,
    /// PID, raw handle number or deserialized receipt cannot construct this type.
    pub(crate) fn peer(&self) -> Result<ExpectedPeer<'a>> {
        let peer = ExpectedPeer::retain(self.child.process())?
            .with_job(self.job)?
            .with_image(self.image)?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        peer.token
            .require_account_runner(&crate::sid::current_user_sid()?, session)?;
        peer.alive()?;
        Ok(peer)
    }

    #[cfg(test)]
    fn resume_fixture(&mut self) -> Result<()> {
        self.resume_once()
    }

    pub(crate) fn resume_recorded(
        &mut self,
        intent: &mut crate::account_state::execution::bootstrap_plan::lifecycle::resume::ResumeIntent<'_, '_, '_, '_>,
    ) -> Result<()> {
        intent.consume(self)?;
        self.resume_once()
    }

    pub(crate) fn resume_paired_recorded(
        &mut self,
        intent: &mut crate::account_state::execution::paired_plan::native::ResumeIntent<
            '_,
            '_,
            '_,
            '_,
        >,
    ) -> Result<()> {
        intent.consume(self)?;
        self.resume_once()
    }

    fn resume_once(&mut self) -> Result<()> {
        ensure!(!self.resumed, "bootstrap resume already attempted");
        self.resumed = true; // Failed preflight/native resume cannot be retried.
        self.peer()?;
        let previous = unsafe { ResumeThread(self.child.thread()) };
        ensure!(
            previous == 1,
            "bootstrap initial suspension differs or resume failed"
        );
        Ok(())
    }
}

#[path = "account_control_pull.rs"]
pub(crate) mod pull;

#[path = "account_bootstrap_entry.rs"]
pub(crate) mod entry;

pub(super) fn os_directory(windows_directory: bool) -> Result<String> {
    use windows::Win32::System::SystemInformation::{GetSystemDirectoryW, GetWindowsDirectoryW};
    let mut buffer = vec![0u16; 32768];
    let length = unsafe {
        if windows_directory {
            GetWindowsDirectoryW(Some(&mut buffer))
        } else {
            GetSystemDirectoryW(Some(&mut buffer))
        }
    } as usize;
    ensure!(
        length > 0 && length < buffer.len(),
        "cannot query bootstrap OS directory"
    );
    Ok(String::from_utf16(&buffer[..length])?)
}

#[cfg(test)]
#[path = "account_control_bootstrap_tests.rs"]
mod tests;
