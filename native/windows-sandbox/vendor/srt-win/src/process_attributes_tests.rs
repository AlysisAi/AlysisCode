use super::*;
use crate::launch::SpawnedChild;
use crate::util::{OwnedHandle, wstr};
use windows::Win32::Foundation::*;
use windows::Win32::Security::SECURITY_ATTRIBUTES;
use windows::Win32::System::Pipes::CreatePipe;
use windows::core::{PCWSTR, PWSTR};

#[test]
fn attribute_owned_values_survive_source_mutation_and_wrapper_move() {
    // A fixed OS image is created suspended and is NEVER resumed. Only token
    // mitigation and inherited-handle observations are made before termination.
    let security = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        bInheritHandle: true.into(),
        ..Default::default()
    };
    let mut read = HANDLE::default();
    let mut write = HANDLE::default();
    unsafe {
        CreatePipe(&mut read, &mut write, Some(&security), 0).unwrap();
    }
    let read = OwnedHandle(read);
    let write = OwnedHandle(write);
    let mut mitigation = 1u64 << 32;
    let mut source = vec![write.raw()];
    let mut attrs = ProcThreadAttrs::new(2).unwrap();
    attrs.set_mitigation_policy(&mitigation).unwrap();
    attrs.set_handle_list(&source).unwrap();
    // Registered values must be independent of later caller-side edits.
    mitigation = 0;
    source[0] = read.raw();
    assert_eq!(mitigation, 0);
    let mut attrs = Box::new(attrs);
    let mut system = vec![0u16; 32768];
    let length = unsafe {
        windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(&mut system))
    } as usize;
    assert!(length > 0 && length < system.len());
    let executable = format!(
        "{}\\cmd.exe",
        String::from_utf16(&system[..length]).unwrap()
    );
    let app = wstr(&executable);
    let mut command = wstr(&format!(
        "{} /d /c exit 0",
        crate::launch::quote_arg(&executable)
    ));
    let mut info = STARTUPINFOEXW::default();
    info.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    info.lpAttributeList = attrs.list();
    let mut pi = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            PCWSTR(app.as_ptr()),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
            None,
            None,
            &info.StartupInfo,
            &mut pi,
        )
        .expect("fixed suspended attribute observation child");
    }
    let mut child = SpawnedChild::new(pi);
    let result = (|| -> Result<()> {
        let mut flags = 0u32;
        unsafe {
            GetProcessMitigationPolicy(
                child.process(),
                ProcessExtensionPointDisablePolicy,
                (&mut flags as *mut u32).cast(),
                size_of::<u32>(),
            )?;
        }
        ensure!(
            flags & 1 != 0,
            "registered mitigation changed with caller variable"
        );
        let mut inherited = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child.process(),
                write.raw(),
                GetCurrentProcess(),
                &mut inherited,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )?;
        }
        let inherited = OwnedHandle(inherited);
        ensure!(
            unsafe { CompareObjectHandles(inherited.raw(), write.raw()) }.as_bool(),
            "original whitelisted handle was not inherited"
        );
        let mut excluded = HANDLE::default();
        match unsafe {
            DuplicateHandle(
                child.process(),
                read.raw(),
                GetCurrentProcess(),
                &mut excluded,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )
        } {
            Ok(()) => {
                let excluded = OwnedHandle(excluded);
                ensure!(
                    !unsafe { CompareObjectHandles(excluded.raw(), read.raw()) }.as_bool(),
                    "caller mutation replaced the inherited handle whitelist"
                );
            }
            Err(error) if error.code() == ERROR_INVALID_HANDLE.to_hresult() => {}
            Err(error) => return Err(error.into()),
        }
        Ok(())
    })();
    unsafe {
        TerminateProcess(child.process(), 0).unwrap();
        assert_eq!(WaitForSingleObject(child.process(), 3000), WAIT_OBJECT_0);
    }
    child.defuse();
    result.unwrap();
}

#[test]
fn attribute_duplicate_and_failed_registration_preserve_prior_buffers() {
    assert!(ProcThreadAttrs::new(0).is_err());
    assert!(ProcThreadAttrs::new(5).is_err());
    let job = Job::new(false).unwrap();
    let mut attrs = ProcThreadAttrs::new(1).unwrap();
    assert!(attrs.set_handle_list(&[]).is_err());
    attrs.set_mitigation_policy(&(1 << 32)).unwrap();
    assert!(attrs.set_mitigation_policy(&0).is_err());
    // One attribute slot is already occupied: failed registration still retains
    // attempted storage and cannot be retried with another buffer.
    assert!(attrs.set_job(&job).is_err());
    assert!(
        attrs
            .set_job(&job)
            .unwrap_err()
            .to_string()
            .contains("already attempted")
    );
    let ptr = attrs.list();
    assert_eq!((ptr.0 as usize) % std::mem::align_of::<usize>(), 0);
    // Drop exercises native deletion with both successful and failed values held.
}
