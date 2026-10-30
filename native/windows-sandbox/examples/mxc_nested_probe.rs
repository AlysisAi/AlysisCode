//! Qualification-only direct SDK caller, deliberately without Alysis path admission.
//! The outer PSEC must prevent this inner request from expanding authority.
use anyhow::{Result, ensure};
use mxc_sdk::{
    mxc_common::{
        cmdline::{CommandLineContext, cmdline_from_argv_for_context},
        logger::{Logger, Mode},
        models::{
            BaseProcessUiConfig, ContainerPolicy, ExecutionRequest, FallbackPolicy, UiPolicy,
        },
        sandbox_process::{SandboxBackend, StdioMode},
    },
    process_container_common::base_container_runner::BaseContainerRunner,
};

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    ensure!(
        args.len() == 3,
        "expected workspace, outside directory, nonce"
    );
    ensure!(
        args[2].len() == 32 && args[2].bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid nonce"
    );
    let system = std::env::var("SYSTEMROOT")?;
    let executable = format!(r"{system}\System32\cmd.exe");
    // Command text contains no path input; CWD is the exact synthetic outside
    // directory. Only a fixed marker file and hexadecimal nonce are used.
    let argv = vec![
        executable,
        "/d".into(),
        "/c".into(),
        format!("echo {}>nested-marker.txt", args[2]),
    ];
    let request = ExecutionRequest {
        script_code: cmdline_from_argv_for_context(
            &argv,
            CommandLineContext::WindowsCreateProcess,
        )?,
        working_directory: args[1].clone(),
        env: Some(vec![
            format!("SYSTEMROOT={system}"),
            format!("LOCALAPPDATA={}", std::env::var("LOCALAPPDATA")?),
        ]),
        inherit_default_env: false,
        policy: ContainerPolicy {
            readonly_paths: vec![system, args[0].clone()],
            readwrite_paths: vec![args[1].clone()],
            fallback: FallbackPolicy {
                allow_dacl_mutation: false,
            },
            ui: UiPolicy {
                disable: false,
                ..Default::default()
            },
            base_process_ui: BaseProcessUiConfig {
                isolation: "container".into(),
                ..Default::default()
            },
            ..Default::default()
        },
        ..Default::default()
    };
    let mut runner = BaseContainerRunner::new();
    let mut logger = Logger::new(Mode::Buffer);
    if runner.validate(&request).is_err() {
        println!("NESTED_VALIDATE_REFUSED");
        return Ok(());
    }
    let mut child = match runner.spawn(&request, &mut logger, StdioMode::Inherit) {
        Ok(child) => child,
        Err(_) => {
            println!("NESTED_SPAWN_REFUSED");
            return Ok(());
        }
    };
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while child.try_wait()?.is_none() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    child.kill()?;
    println!("NESTED_CHILD_EXIT={}", child.wait()?);
    Ok(())
}
