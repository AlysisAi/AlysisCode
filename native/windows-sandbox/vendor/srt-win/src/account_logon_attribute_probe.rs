//! Manual diagnostic only. Synthetic NETCREDENTIALS_ONLY does not authenticate
//! an account or exercise network traffic. Both children remain suspended.
use super::*;

#[test]
#[ignore = "bounded manual native API diagnostic; not separate-account qualification"]
fn bootstrap_secondary_logon_extended_job_attribute_diagnostic() {
    let image = image();
    let app = wstr(image.application().unwrap().to_str().unwrap());
    let directory = wstr(&os_directory(false).unwrap());
    let mut outcomes = Vec::new();
    for extended in [false, true] {
        let job = Job::new(false).unwrap();
        containment::prepare(&job).unwrap();
        let mut attributes = ProcThreadAttrs::new(1).unwrap();
        attributes.set_job(&job).unwrap();
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = if extended {
            size_of::<STARTUPINFOEXW>()
        } else {
            size_of::<STARTUPINFOW>()
        } as u32;
        startup.lpAttributeList = attributes.list();
        let mut command = wstr(&crate::launch::quote_arg(
            image.application().unwrap().to_str().unwrap(),
        ));
        let mut info = PROCESS_INFORMATION::default();
        let result = unsafe {
            CreateProcessWithLogonW(
                pcwstr(&wstr("AlysisSyntheticNeverValidated")),
                pcwstr(&wstr(".")),
                pcwstr(&wstr("NotARealCredential!SyntheticOnly")),
                LOGON_NETCREDENTIALS_ONLY,
                pcwstr(&app),
                Some(windows::core::PWSTR(command.as_mut_ptr())),
                CREATE_SUSPENDED
                    | CREATE_NO_WINDOW
                    | if extended {
                        EXTENDED_STARTUPINFO_PRESENT
                    } else {
                        PROCESS_CREATION_FLAGS(0)
                    },
                None,
                pcwstr(&directory),
                &startup.StartupInfo,
                &mut info,
            )
        };
        match result {
            Ok(()) => {
                let child = SpawnedChild::new(info);
                image.verify_process(child.process()).unwrap();
                let mut member = windows::core::BOOL::default();
                unsafe {
                    windows::Win32::System::JobObjects::IsProcessInJob(
                        child.process(),
                        Some(job.raw()),
                        &mut member,
                    )
                }
                .unwrap();
                assert_eq!(
                    member.as_bool(),
                    extended,
                    "extended job list was ignored or baseline unexpectedly assigned"
                );
                outcomes.push(serde_json::json!({"extended":extended,"created":true,"job_member":member.as_bool(),"hresult":null}));
                drop(child);
            }
            Err(error) => {
                assert!(extended, "basic synthetic API control failed: {error}");
                outcomes.push(serde_json::json!({"extended":extended,"created":false,"job_member":null,"hresult":error.code().0}));
            }
        }
        drop(attributes);
        drop(job);
    }
    eprintln!(
        "secondary_logon_attribute_observation={}",
        serde_json::json!({
        "cases":outcomes,"synthetic_credentials_unvalidated":true,"children_never_resumed":true,
        "separate_account_qualified":false,"inflight_death_qualified":false,"production_ready":false})
    );
}
