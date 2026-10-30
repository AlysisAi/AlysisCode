param(
    [string]$Python = 'python',
    [switch]$Installed,
    [switch]$Internet,
    [switch]$RequireAllChecks,
    [switch]$HostDiagnostics
)

# Run from a normal Windows terminal. Provisioning/UAC is a separate step.
$ErrorActionPreference = 'Stop'
if ($Internet -and -not $Installed) { throw '-Internet requires -Installed.' }
if ($RequireAllChecks -and (-not $Installed -or -not $Internet)) {
    throw '-RequireAllChecks requires -Installed and -Internet; omitted checks cannot establish compatibility.'
}
$pendingJournal = $null
if ($Installed) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA) -or
        -not [System.IO.Path]::IsPathRooted($env:LOCALAPPDATA) -or
        [System.IO.Path]::GetPathRoot($env:LOCALAPPDATA).Length -lt 3) {
        throw 'Installed tests require an absolute LOCALAPPDATA path.'
    }
    $pendingJournal = Join-Path $env:LOCALAPPDATA 'alysis-sandbox/execution-pending.json'
    # Check before pytest can rotate old temporary directories: a failed run's
    # intact workspace may be needed for exact-identity recovery.
    if (Test-Path -LiteralPath $pendingJournal -ErrorAction Stop) {
        throw 'Unfinished sandbox execution: preserve its workspace and run recover-native before rerunning installed tests.'
    }
}
$repoRoot = Split-Path -Parent $PSScriptRoot
$variables = @(
    'PYTHONPATH', 'ALYSIS_WINDOWS_SANDBOX_HELPER', 'ALYSIS_WINDOWS_SANDBOX_SHA256',
    'ALYSIS_RUN_INSTALLED_SANDBOX_TESTS', 'ALYSIS_SHELL_SANDBOX_BACKEND',
    'ALYSIS_SHELL_SANDBOX_MODE', 'ALYSIS_SHELL_SANDBOX_NETWORK',
    'ALYSIS_RUN_SANDBOX_NETWORK_TESTS'
)
$saved = @{}
foreach ($name in $variables) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
Push-Location -LiteralPath $repoRoot
$ownerScope = $null
try {
    . (Join-Path $PSScriptRoot 'windows_test_owner_scope.ps1')
    $env:PYTHONPATH = Join-Path $repoRoot 'src'
    # Exercise restoration before normalizing this process's default owner;
    # otherwise elevated CI children would already inherit the normalized owner.
    & $Python -m pytest tests/test_windows_sandbox_test_harness.py -q --tb=short
    if ($LASTEXITCODE -ne 0) { throw 'Sandbox owner-scope regression tests failed.' }
    $ownerScope = [AlysisSandboxTests.DefaultOwnerScope]::EnterCurrentUser()
    $regressions = @(
        'tests/test_windows_sandbox.py', 'tests/test_windows_sandbox_doctor.py',
        'tests/test_windows_sandbox_https_probe.py',
        'tests/test_sandbox_settings_background.py',
        'tests/test_sandbox_doctor.py', 'tests/test_sandbox_cli.py',
        'tests/test_shell_sandbox.py', 'tests/test_background_runner_sandbox.py',
        'tests/test_sandbox_filesystem_policy.py',
        'tests/test_sandbox_git_writes.py', 'tests/test_bounded_capture.py',
        'tests/test_task_scope.py', 'tests/test_sandbox_approval_fallback.py',
        'tests/test_ide_sandbox_profile.py', 'tests/test_windows_managed_session.py',
        'tests/test_managed_session_windows.py', 'tests/test_native_approval_routing.py',
        'tests/test_managed_session_persistence.py', 'tests/test_managed_session_recovery.py',
        'tests/test_command_text.py',
        'tests/test_sandbox_bookkeeping.py', 'tests/test_windows_sandbox_bookkeeping.py',
        'tests/test_command_text_usage.py', 'tests/test_desktop_run_contract.py',
        'tests/test_ide_protocol.py', 'tests/test_task_outcome_authority.py',
        'tests/test_shell_verification_recovery.py', 'tests/test_verification_batch_recovery.py',
        'tests/test_host_actions.py', 'tests/test_approval_review.py',
        'tests/test_command_review_rules.py', 'tests/test_read_only_commands.py',
        'tests/test_critical_paths.py', 'tests/test_ide_permission_bridge.py',
        'tests/test_agent_loop_review_approval.py'
    )
    & $Python -m pytest @regressions -q --tb=short
    if ($LASTEXITCODE -ne 0) { throw 'Sandbox regression tests failed.' }
    if ($Installed) {
        if ($HostDiagnostics) {
            & (Join-Path $PSScriptRoot 'windows_sandbox_host_diagnostics.ps1')
        }
        $bundle = Join-Path $repoRoot 'native/windows-sandbox/target/bundle/release'
        $manifest = Get-Content -LiteralPath (Join-Path $bundle 'manifest.json') -Raw | ConvertFrom-Json
        $env:ALYSIS_WINDOWS_SANDBOX_HELPER = Join-Path $bundle 'alysis-sandbox.exe'
        $env:ALYSIS_WINDOWS_SANDBOX_SHA256 = $manifest.sha256
        $env:ALYSIS_RUN_INSTALLED_SANDBOX_TESTS = '1'
        $env:ALYSIS_RUN_SANDBOX_NETWORK_TESTS = if ($Internet) { '1' } else { '0' }
        $env:ALYSIS_SHELL_SANDBOX_BACKEND = 'windows-native'
        $env:ALYSIS_SHELL_SANDBOX_MODE = 'strict'
        $env:ALYSIS_SHELL_SANDBOX_NETWORK = 'off'
        & $Python -m alysis_code sandbox doctor --smoke
        if ($LASTEXITCODE -ne 0) { throw 'Installed sandbox smoke check failed.' }
        if ($Internet) {
            $env:ALYSIS_SHELL_SANDBOX_NETWORK = 'on'
            & $Python -m alysis_code sandbox doctor --smoke
            if ($LASTEXITCODE -ne 0) { throw 'Online sandbox launch smoke check failed.' }
            $env:ALYSIS_SHELL_SANDBOX_NETWORK = 'off'
        }
        & $Python -m pytest tests/test_windows_sandbox_installed.py tests/test_windows_sandbox_agent_installed.py tests/test_windows_sandbox_registry_installed.py tests/test_windows_sandbox_nested_helper_installed.py -q --tb=short -rx `
            --junitxml=native/windows-sandbox/target/installed-release-tests.xml
        if (Test-Path -LiteralPath $pendingJournal -ErrorAction Stop) {
            throw 'Installed tests left a recovery journal. Preserve test workspaces, inspect the report and run recover-native before retrying.'
        }
        if ($LASTEXITCODE -ne 0) { throw 'Installed sandbox integration tests failed.' }
        if ($RequireAllChecks) {
            $reportPath = Join-Path $repoRoot 'native/windows-sandbox/target/installed-release-tests.xml'
            $xmlSettings = [System.Xml.XmlReaderSettings]::new()
            $xmlSettings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
            $xmlSettings.XmlResolver = $null
            $xmlReader = [System.Xml.XmlReader]::Create($reportPath, $xmlSettings)
            try {
                $report = [System.Xml.XmlDocument]::new()
                $report.Load($xmlReader)
                if ($report.SelectNodes('//testcase').Count -eq 0 -or
                    $report.SelectNodes('//testcase/skipped | //testcase/failure | //testcase/error').Count -ne 0) {
                    throw 'Installed compatibility gate failed: every check must run and pass. Expected failures (xfail) and skips are not release evidence.'
                }
            }
            finally { $xmlReader.Dispose() }
        }
    }
}
finally {
    try {
        if ($null -ne $ownerScope) { $ownerScope.Dispose() }
    }
    finally {
        foreach ($name in $variables) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
        }
        Pop-Location
    }
}
