// The implementation and its license live in vendor/srt-win. Alysis uses its own
// installation identity; it never reads or modifies another product's sandbox.
#[cfg(windows)]
mod container_policy;
#[cfg(all(windows, feature = "mxc"))]
mod mxc_policy;
#[cfg(windows)]
mod policy;

fn main() {
    #[cfg(windows)]
    {
        #[cfg(feature = "mxc")]
        if let Some(result) = mxc_policy::dispatch(std::env::args().nth(1).as_deref()) {
            let code = match result {
                Ok(code) => code,
                Err(error) => {
                    eprintln!("alysis-sandbox: {error:#}");
                    125
                }
            };
            std::process::exit(code);
        }
        if std::env::args().nth(1).as_deref() == Some("protocol") {
            println!(
                r#"{{"protocol":3,"security_revision":4,"implementation":"alysis-windows-sandbox","version":"0.3.3","containment":"appcontainer-write-restricted","network_modes":["off","on"],"qualification":"experimental"}}"#
            );
            return;
        }
        let operation = std::env::args().nth(1);
        if matches!(operation.as_deref(), Some("run-request" | "recover")) {
            let outcome = if operation.as_deref() == Some("recover") {
                policy::recover()
            } else {
                policy::run()
            };
            let code = match outcome {
                Ok(code) => code,
                Err(error) => {
                    eprintln!("alysis-sandbox: {error:#}");
                    125
                }
            };
            std::process::exit(code);
        }
        std::process::exit(srt_win::run_from_args(std::env::args_os()));
    }
    #[cfg(not(windows))]
    {
        eprintln!("alysis-sandbox requires native Windows; use the Linux backend in WSL2");
        std::process::exit(1);
    }
}
