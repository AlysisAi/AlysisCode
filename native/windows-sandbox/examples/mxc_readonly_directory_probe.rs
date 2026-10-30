//! Qualification-only direct Microsoft SDK replay. Never shipped as the helper.
//! Runs only the bounded Python directory-open fixture, without Alysis policy
//! translation or host ACL changes, to separate OS behavior from the adapter.
use anyhow::{Result, ensure};
use mxc_sdk::{
    mxc_common::{
        cmdline::{CommandLineContext, cmdline_from_argv_for_context},
        logger::{Logger, Mode},
        models::{BaseProcessUiConfig, ContainerPolicy, ExecutionRequest, FallbackPolicy,
            NetworkAction, NetworkEgressPolicy, NetworkIngressPolicy, UiPolicy},
        sandbox_process::{SandboxBackend, StdioMode},
    },
    process_container_common::base_container_runner::BaseContainerRunner,
};

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    ensure!(args.len() == 5, "expected python, base Python root, workspace, private canary directory, mode");
    ensure!(matches!(args[4].as_str(), "ro-v1" | "ro-v11" | "rw" | "ro-parent" | "enum-parent" | "ro-volume" | "enum-ancestors"), "invalid mode");
    let python = std::path::Path::new(&args[0]);
    let root = std::path::Path::new(&args[2]);
    let probe = root.join("native-readonly-directory-probe.py");
    ensure!(python.is_absolute() && root.is_absolute() && probe.is_file(), "missing bounded fixture");
    let system = std::env::var("SYSTEMROOT")?;
    let mut reads = vec![
        args[1].clone(),
        python.parent().and_then(|p| p.parent()).unwrap().to_string_lossy().into_owned(),
    ];
    if args[4] != "rw" { reads.push(args[2].clone()); }
    // Differential probes widen only to the caller's synthetic fixture parent.
    // These settings are never used by the shipping adapter.
    if args[4] == "ro-parent" {
        reads.push(root.parent().unwrap().to_string_lossy().into_owned());
    }
    // Upstream #1109 suggests a volume-root grant; current OS behavior must be
    // measured, never assumed to be metadata-only or used by the product.
    if args[4] == "ro-volume" {
        reads.push(root.ancestors().last().unwrap().to_string_lossy().into_owned());
    }
    for name in ["System32", "WinSxS", "Microsoft.NET", "assembly", "Globalization"] {
        let path = std::path::Path::new(&system).join(name);
        if path.is_dir() { reads.push(path.to_string_lossy().into_owned()); }
    }
    let argv = vec![args[0].clone(), "-I".into(), "-B".into(), probe.to_string_lossy().into_owned(), args[2].clone()];
    let request = ExecutionRequest {
        script_code: cmdline_from_argv_for_context(&argv, CommandLineContext::WindowsCreateProcess)?,
        working_directory: args[2].clone(),
        env: Some(vec![format!("SYSTEMROOT={system}"), format!("LOCALAPPDATA={}", std::env::var("LOCALAPPDATA")?)]),
        inherit_default_env: false,
        policy: ContainerPolicy {
            readonly_paths: reads,
            enumerate_paths: if args[4] == "enum-parent" {
                vec![root.parent().unwrap().to_string_lossy().into_owned()]
            } else if args[4] == "enum-ancestors" {
                root.ancestors().skip(1).map(|p| p.to_string_lossy().into_owned()).collect()
            } else { vec![] },
            readwrite_paths: if args[4] == "rw" { vec![args[2].clone()] } else { vec![] },
            denied_paths: if args[4] == "ro-v1" { vec![] } else { vec![args[3].clone()] },
            capabilities: vec!["registryRead".into()],
            fallback: FallbackPolicy { allow_dacl_mutation: false },
            network_egress: Some(NetworkEgressPolicy { default: NetworkAction::Deny, ..Default::default() }),
            network_ingress: Some(NetworkIngressPolicy { default: NetworkAction::Deny, host_loopback: NetworkAction::Deny }),
            network_specified: true,
            network_mode_specified: true,
            ui: UiPolicy { disable: false, ..Default::default() },
            base_process_ui: BaseProcessUiConfig { isolation: "container".into(), ..Default::default() },
            ..Default::default()
        },
        ..Default::default()
    };
    let mut runner = BaseContainerRunner::new();
    runner.validate(&request).map_err(|_| anyhow::anyhow!("direct SDK validation refused"))?;
    let mut logger = Logger::new(Mode::Buffer);
    let mut child = runner.spawn(&request, &mut logger, StdioMode::Inherit)
        .map_err(|_| anyhow::anyhow!("direct SDK launch refused"))?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
    while child.try_wait()?.is_none() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    child.kill()?;
    ensure!(child.wait()? == 0, "bounded child failed");
    Ok(())
}
