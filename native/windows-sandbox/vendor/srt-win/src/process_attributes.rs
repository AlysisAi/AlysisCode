//! Attribute-list storage and every value buffer survive until list deletion.
//! Raw kernel handles remain caller-owned; job and capability structure borrows
//! explicitly constrain the wrapper lifetime. This module never launches.

use crate::job::Job;
use anyhow::{Context, Result, ensure};
use std::marker::PhantomData;
use std::mem::size_of;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::Security::SECURITY_CAPABILITIES;
use windows::Win32::System::Threading::*;

pub(crate) struct ProcThreadAttrs<'a> {
    storage: Vec<usize>,
    mitigation: Option<Box<u64>>,
    handles: Option<Box<[HANDLE]>>,
    job: Option<Box<[HANDLE; 1]>>,
    capabilities_set: bool,
    _referents: PhantomData<&'a ()>,
}

impl<'a> ProcThreadAttrs<'a> {
    pub(crate) fn new(count: u32) -> Result<Self> {
        ensure!(
            (1..=4).contains(&count),
            "unsupported process attribute count"
        );
        let mut bytes = 0usize;
        unsafe {
            let _ = InitializeProcThreadAttributeList(None, count, None, &mut bytes);
        }
        ensure!(
            bytes > 0 && bytes <= 65536,
            "invalid process attribute allocation size"
        );
        let mut storage = vec![0usize; bytes.div_ceil(size_of::<usize>())];
        unsafe {
            InitializeProcThreadAttributeList(
                Some(LPPROC_THREAD_ATTRIBUTE_LIST(storage.as_mut_ptr().cast())),
                count,
                None,
                &mut bytes,
            )
            .context("InitializeProcThreadAttributeList")?;
        }
        Ok(Self {
            storage,
            mitigation: None,
            handles: None,
            job: None,
            capabilities_set: false,
            _referents: PhantomData,
        })
    }

    pub(crate) fn list(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        LPPROC_THREAD_ATTRIBUTE_LIST(self.storage.as_mut_ptr().cast())
    }

    pub(crate) fn set_mitigation_policy(&mut self, policy: &u64) -> Result<()> {
        ensure!(
            self.mitigation.is_none(),
            "mitigation attribute already attempted"
        );
        // Retain even after an API error; never replace memory possibly borrowed
        // by a partially updated native list.
        self.mitigation = Some(Box::new(*policy));
        let pointer = self.mitigation.as_deref().unwrap() as *const u64;
        unsafe {
            UpdateProcThreadAttribute(
                self.list(),
                0,
                PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY as usize,
                Some(pointer.cast()),
                size_of::<u64>(),
                None,
                None,
            )
            .context("UpdateProcThreadAttribute(MITIGATION_POLICY)")
        }
    }

    /// Own the list values, not the kernel handles. The caller retains those
    /// handles through process creation and marks inherited handles explicitly.
    pub(crate) fn set_handle_list(&mut self, handles: &[HANDLE]) -> Result<()> {
        ensure!(self.handles.is_none(), "handle attribute already attempted");
        ensure!(!handles.is_empty(), "empty inherited handle list");
        self.handles = Some(handles.to_vec().into_boxed_slice());
        let values = self.handles.as_deref().unwrap();
        let pointer = values.as_ptr();
        let bytes = std::mem::size_of_val(values);
        unsafe {
            UpdateProcThreadAttribute(
                self.list(),
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                Some(pointer.cast()),
                bytes,
                None,
                None,
            )
            .context("UpdateProcThreadAttribute(HANDLE_LIST)")
        }
    }

    pub(crate) fn set_job(&mut self, job: &'a Job) -> Result<()> {
        ensure!(self.job.is_none(), "job attribute already attempted");
        self.job = Some(Box::new([job.raw()]));
        let pointer = self.job.as_deref().unwrap().as_ptr();
        unsafe {
            UpdateProcThreadAttribute(
                self.list(),
                0,
                PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
                Some(pointer.cast()),
                size_of::<[HANDLE; 1]>(),
                None,
                None,
            )
            .context("install atomic containment job")
        }
    }

    /// # Safety
    /// All SID pointers inside capabilities, including the capability array,
    /// must remain valid and immutable until this attribute list is dropped.
    /// Borrowing the outer structure cannot express those foreign pointer
    /// lifetimes; each caller retains its original SID allocations.
    pub(crate) unsafe fn set_security_capabilities(
        &mut self,
        capabilities: &'a SECURITY_CAPABILITIES,
    ) -> Result<()> {
        ensure!(
            !self.capabilities_set,
            "capability attribute already attempted"
        );
        self.capabilities_set = true;
        unsafe {
            UpdateProcThreadAttribute(
                self.list(),
                0,
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
                Some((capabilities as *const SECURITY_CAPABILITIES).cast()),
                size_of::<SECURITY_CAPABILITIES>(),
                None,
                None,
            )
            .context("install AppContainer network capability policy")
        }
    }
}

impl Drop for ProcThreadAttrs<'_> {
    fn drop(&mut self) {
        // Rust drops fields only after this body, retaining all value buffers
        // through native list deletion, including error unwinding.
        unsafe {
            DeleteProcThreadAttributeList(self.list());
        }
    }
}

#[cfg(test)]
#[path = "process_attributes_tests.rs"]
mod tests;
