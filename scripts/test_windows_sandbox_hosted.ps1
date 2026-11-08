param(
    [switch]$Internet,
    [switch]$RequireAllChecks,
    [switch]$Cleanup
)

# GitHub's worker job forbids child breakaway. Qualify the desktop sandbox from
# the logged-on runner's normal session, without changing either job's limits.
# This launcher is only for disposable GitHub-hosted Windows qualification.
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or
    $env:RUNNER_OS -ne 'Windows' -or $env:GITHUB_RUN_ID -notmatch '^\d+$' -or
    $env:GITHUB_RUN_ATTEMPT -notmatch '^\d+$') {
    throw 'Hosted sandbox qualification requires a disposable GitHub Windows runner.'
}
$taskName = "AlysisSandboxQualification-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"

function Remove-QualificationTask {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        if ($task.State -in @('Running', 'Queued')) { Stop-ScheduledTask -InputObject $task }
        Unregister-ScheduledTask -InputObject $task -Confirm:$false
    }
}

if ($Cleanup) {
    Remove-QualificationTask
    return
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'A qualification task already exists; preserve its evidence and clean it up first.'
}
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = (Resolve-Path (Join-Path $repoRoot '.venv/Scripts/python.exe')).Path
$log = Join-Path $repoRoot 'native/windows-sandbox/target/hosted-qualification.log'
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$pwsh = (Get-Process -Id $PID).Path

function Quote-Literal([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }

# Only explicit test inputs cross the scheduler boundary, never the runner's
# credential-bearing environment. Keep the original user and interactive token.
$command = @"
`$ErrorActionPreference = 'Stop'
& {
    try {
        if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $(Quote-Literal $userSid)) {
            throw 'Scheduled qualification changed the runner identity.'
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
$action = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoProfile -NonInteractive -EncodedCommand $encoded" -WorkingDirectory $repoRoot
$principal = New-ScheduledTaskPrincipal -UserId $userSid -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
try {
    $null = Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings
    Start-ScheduledTask -TaskName $taskName
    $deadline = [DateTime]::UtcNow.AddMinutes(21)
    do {
        Start-Sleep -Seconds 2
        $task = Get-ScheduledTask -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        if ($info.LastRunTime.Year -gt 2000 -and $task.State -ne 'Running' -and $task.State -ne 'Queued') {
            if ($info.LastTaskResult -ne 0 -or -not (Test-Path -LiteralPath $log)) {
                throw "Scheduled sandbox qualification failed: $($info.LastTaskResult)."
            }
            return
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Scheduled sandbox qualification exceeded its time limit.'
} finally {
    Remove-QualificationTask
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
}
