# Runs inside the sandbox: credentials, host brokers and ways to leave something
# behind outside the project. One line per attempt in results.log:
# PROBE|name|OK|detail when the attempt went through, PROBE|name|ERR|detail when refused.
param([string]$SpecPath)
$ErrorActionPreference = 'Stop'
$spec = Get-Content -Raw -LiteralPath $SpecPath | ConvertFrom-Json
$log = Join-Path $PWD 'results.log'
function Attempt([string]$name, [scriptblock]$probe) {
    try {
        $detail = ((& $probe 2>&1 | Out-String) -replace '\s+', ' ').Trim()
        Add-Content -LiteralPath $log -Value "PROBE|$name|OK|$detail"
    } catch {
        Add-Content -LiteralPath $log -Value ("PROBE|$name|ERR|" + ($_.Exception.Message -replace '\s+', ' '))
    }
}
$port = $spec.port
$outside = $spec.outside

# ---- credentials ------------------------------------------------------------
Attempt 'environment_names' { (Get-ChildItem env: | ForEach-Object Name) -join ',' }
Attempt 'environment_secret' { if ($env:ALYSIS_PROBE_SECRET) { 'VISIBLE ' + $env:ALYSIS_PROBE_SECRET } else { throw 'not in the environment' } }
Attempt 'registry_saved_variable' { (Get-ItemProperty -Path 'HKCU:\Environment' -Name $spec.variable).($spec.variable) }
Add-Type -Namespace Probe -Name Native -MemberDefinition @'
[DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
public static extern bool CredEnumerateW(string filter, int flags, out int count, out IntPtr creds);
[DllImport("advapi32.dll")] public static extern void CredFree(IntPtr buffer);
[DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
'@
Attempt 'credential_manager' {
    $count = 0; $pointer = [IntPtr]::Zero
    if (-not [Probe.Native]::CredEnumerateW($null, 0, [ref]$count, [ref]$pointer)) { throw 'refused' }
    [Probe.Native]::CredFree($pointer); "ENUMERATED $count"
}
Attempt 'dpapi_user_key' {
    Add-Type -AssemblyName System.Security
    $plain = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($spec.dpapi_blob), $null, 'CurrentUser')
    [Text.Encoding]::UTF8.GetString($plain)
}
Attempt 'outside_file_read' { Get-Content -Raw -LiteralPath (Join-Path $outside 'canary.txt') }

# ---- host brokers -----------------------------------------------------------
Attempt 'wmi_process_create' {
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd /c echo x> `"$outside\wmi.txt`"" }
    "ReturnValue=$($r.ReturnValue)"
}
Attempt 'scheduled_task' {
    $o = schtasks /create /f /tn $spec.task /tr "cmd /c echo x> `"$outside\task.txt`"" /sc once /st 23:59 2>&1 | Out-String
    if ($LASTEXITCODE) { throw $o } else { 'CREATED' }
}
Attempt 'shell_windows_com' { [Activator]::CreateInstance([type]::GetTypeFromCLSID('9BA05972-F6A8-11CF-A442-00A0C90A8F39')).Count }
Attempt 'shell_execute_com' { (New-Object -ComObject Shell.Application).ShellExecute('cmd.exe', "/c echo x> `"$outside\shellexecute.txt`"", '', 'open', 0); 'called' }
Attempt 'bits_transfer' { Import-Module BitsTransfer; Start-BitsTransfer -Source "http://127.0.0.1:$port/bits" -Destination (Join-Path $PWD 'bits.out'); 'TRANSFERRED' }
Attempt 'winhttp_com' { $o = New-Object -ComObject WinHttp.WinHttpRequest.5.1; $o.Open('GET', "http://127.0.0.1:$port/winhttp", $false); $o.Send(); $o.Status }
Attempt 'web_request' { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:$port/iwr").StatusCode }
Attempt 'open_url_in_host_browser' { Start-Process "http://127.0.0.1:$port/start-process"; 'called' }
Attempt 'explorer_url' { $p = Start-Process explorer.exe -ArgumentList "http://127.0.0.1:$port/explorer" -PassThru; Start-Sleep 2; 'started' }
Attempt 'child_process_outside_write' { Start-Process cmd.exe -ArgumentList "/c echo x> `"$outside\child.txt`"" -Wait; 'called' }
foreach ($right in @(@('write_memory', 0x0020), @('duplicate_handles', 0x0040), @('terminate', 0x0001))) {
    Attempt ("host_process_" + $right[0]) {
        $handle = [Probe.Native]::OpenProcess([uint32]$right[1], $false, [uint32]$spec.host_pid)
        if ($handle -eq [IntPtr]::Zero) { throw 'refused' }
        $null = [Probe.Native]::CloseHandle($handle); 'OPENED'
    }
}

# ---- leaving something behind outside the project -----------------------------
Attempt 'registry_run_key' { Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $spec.task -Value 'cmd /c echo x'; 'WRITTEN' }
Attempt 'registry_saved_variable_write' { Set-ItemProperty -Path 'HKCU:\Environment' -Name ($spec.task + 'W') -Value 'x'; 'WRITTEN' }
foreach ($target in $spec.write_paths) {
    Attempt ("outside_write:" + $target.label) { Set-Content -LiteralPath $target.path -Value 'sandbox'; 'WRITTEN' }
}
Attempt 'project_write' { Set-Content -LiteralPath (Join-Path $PWD 'inside.txt') -Value 'sandbox'; 'WRITTEN' }
