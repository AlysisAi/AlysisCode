param(
    [switch]$Internet,
    [switch]$RequireAllChecks
)

# GitHub and Task Scheduler jobs forbid child breakaway. Qualify from the
# existing desktop shell, leaving those jobs and native sandbox limits unchanged.
# This launcher is only for disposable GitHub-hosted Windows qualification.
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or
    $env:RUNNER_OS -ne 'Windows' -or $env:GITHUB_RUN_ID -notmatch '^\d+$' -or
    $env:GITHUB_RUN_ATTEMPT -notmatch '^\d+$') {
    throw 'Hosted sandbox qualification requires a disposable GitHub Windows runner.'
}
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = (Resolve-Path (Join-Path $repoRoot '.venv/Scripts/python.exe')).Path
$log = Join-Path $repoRoot 'native/windows-sandbox/target/hosted-qualification.log'
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$pwsh = (Get-Process -Id $PID).Path

function Quote-Literal([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }

# Only explicit test inputs and host paths cross the launch boundary. Keep the
# original user and verify that identity before running any qualification tests.
$command = @"
`$ErrorActionPreference = 'Stop'
& {
    try {
        if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $(Quote-Literal $userSid)) {
            throw 'Desktop qualification changed the runner identity.'
        }
        & $(Quote-Literal (Join-Path $PSScriptRoot 'test_windows_sandbox.ps1')) -Installed -HostDiagnostics -Internet:`$$($Internet.IsPresent.ToString().ToLowerInvariant()) -RequireAllChecks:`$$($RequireAllChecks.IsPresent.ToString().ToLowerInvariant()) -Python $(Quote-Literal $python)
        exit 0
    } catch {
        Write-Output `$_
        exit 1
    }
} *> $(Quote-Literal $log)
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
# Do not forward GitHub tokens, provider credentials, or other runner secrets.
$hostEnvironment = @{}
foreach ($name in @('SystemRoot', 'SystemDrive', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT',
        'OS', 'USERNAME', 'USERDOMAIN',
        'TEMP', 'TMP', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA',
        'LOCALAPPDATA', 'PROGRAMDATA', 'ProgramFiles', 'ProgramFiles(x86)',
        'PSModulePath', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS')) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($null -ne $value) { $hostEnvironment[$name] = $value }
}
# PowerShell uses PATHEXT even for explicit executable paths. Without it,
# Python can be opened through ShellExecute without output or an exit status.
if (($hostEnvironment['PATHEXT'] -split ';') -notcontains '.EXE') {
    throw 'The host PATHEXT must identify .EXE as a native executable.'
}
$environmentBlock = (($hostEnvironment.Keys | Sort-Object | ForEach-Object {
    "$_=$($hostEnvironment[$_])"
}) -join "`0") + "`0`0"
. (Join-Path $PSScriptRoot 'windows_desktop_test_host.ps1')
try {
    $exitCode = [AlysisDesktopTestHost]::Run($pwsh, "-NoProfile -NonInteractive -EncodedCommand $encoded", $repoRoot, $environmentBlock)
    if ($exitCode -ne 0 -or -not (Test-Path -LiteralPath $log)) {
        throw "Desktop sandbox qualification failed: $exitCode."
    }
} finally {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
}
