param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Setup', 'Registry', 'Validate', 'Cleanup', 'Wmi')]
    [string]$Action,
    [Parameter(Mandatory = $true)][string]$Config
)
$ErrorActionPreference = 'Stop'
$configData = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json

function Error-Result($ErrorRecord) {
    $exception = $ErrorRecord.Exception
    while ($null -ne $exception.InnerException) { $exception = $exception.InnerException }
    $kind = 'unexpected_error'
    if (($exception -is [System.UnauthorizedAccessException]) -or
        ($exception -is [System.Security.SecurityException]) -or
        ($exception.HResult -eq -2147024891) -or  # E_ACCESSDENIED
        ($exception.HResult -eq -2147217405) -or  # WBEM_E_ACCESS_DENIED
        (($exception -is [System.Management.ManagementException]) -and
            $exception.ErrorCode -eq [System.Management.ManagementStatus]::AccessDenied)) {
        $kind = 'access_denied'
    }
    return @{ result = $kind; error_type = $exception.GetType().FullName; hresult = $exception.HResult }
}

function Private-Acl($Key, $Sid) {
    $acl = $Key.GetAccessControl()
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    return ($acl.AreAccessRulesProtected -and $rules.Count -eq 1 -and
        $rules[0].IdentityReference.Value -eq $Sid.Value -and
        $rules[0].AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
        $rules[0].RegistryRights -eq [System.Security.AccessControl.RegistryRights]::FullControl -and
        -not $rules[0].IsInherited)
}

if ($Action -eq 'Wmi') {
    $processClass = $null
    $inParams = $null
    $outParams = $null
    try {
        # Local DCOM/WMI, with method-level and external process deadlines.
        $processClass = [wmiclass]'\\.\root\cimv2:Win32_Process'
        $inParams = $processClass.GetMethodParameters('Create')
        $inParams['CommandLine'] = [string]$configData.command
        $inParams['CurrentDirectory'] = [string]$configData.cwd
        $options = New-Object System.Management.InvokeMethodOptions
        $options.Timeout = [TimeSpan]::FromSeconds(5)
        $outParams = $processClass.InvokeMethod('Create', $inParams, $options)
        @{ action = $Action; result = 'method_returned'; return_value = [uint32]$outParams['ReturnValue'];
            process_id = [uint32]$outParams['ProcessId'] } | ConvertTo-Json -Compress
    } catch {
        $result = Error-Result $_
        $result.action = $Action
        $result | ConvertTo-Json -Compress
    } finally {
        if ($null -ne $outParams) { $outParams.Dispose() }
        if ($null -ne $inParams) { $inParams.Dispose() }
        if ($null -ne $processClass) { $processClass.Dispose() }
    }
    exit 0
}

# Never accept an arbitrary registry target or recursively remove a registry tree.
$subkey = [string]$configData.subkey
if ($subkey -notmatch '^Software\\AlysisMxcPlatform_[0-9a-f]{32}$' -or
    $configData.owner_nonce -notmatch '^[0-9a-f]{64}$' -or
    $configData.canary -notmatch '^[0-9a-f]{64}$') { throw 'Invalid synthetic registry identity' }
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$root = [Microsoft.Win32.Registry]::CurrentUser
$key = $null
try {
    if ($Action -eq 'Setup') {
        $key = $root.OpenSubKey($subkey)
        if ($null -ne $key) { throw 'Synthetic registry leaf already exists' }
        $acl = New-Object System.Security.AccessControl.RegistrySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($sid)
        $rule = New-Object System.Security.AccessControl.RegistryAccessRule(
            $sid, [System.Security.AccessControl.RegistryRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]::None,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
        $key = $root.CreateSubKey($subkey,
            [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, $acl)
        $key.SetValue('OwnerNonce', [string]$configData.owner_nonce)
        $key.SetValue('Canary', [string]$configData.canary)
        $key.SetValue('Mutable', [string]$configData.mutable)
        @{ action = $Action; created = $true; sid = $sid.Value;
            private_acl = (Private-Acl $key $sid) } | ConvertTo-Json -Compress
    } elseif ($Action -eq 'Registry') {
        if ($configData.root -eq 'HKU') {
            if ($configData.sid -notmatch '^S-1-(\d+-)*\d+$') { throw 'Invalid owner SID' }
            $root = [Microsoft.Win32.Registry]::Users
            $subkey = [string]$configData.sid + '\' + $subkey
        } elseif ($configData.root -ne 'HKCU') { throw 'Invalid synthetic root' }
        $result = @{ action = $Action; read_succeeded = $false; canary_exact = $false;
            write_succeeded = $false; read_result = 'not_found'; write_result = 'not_found' }
        try {
            $key = $root.OpenSubKey($subkey, $false)
            if ($null -ne $key) {
                $value = $key.GetValue('Canary', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $result.read_succeeded = $null -ne $value
                $result.canary_exact = $value -ceq [string]$configData.canary
                $result.read_result = 'opened'
            }
        } catch {
            $errorResult = Error-Result $_
            $result.read_result = $errorResult.result
            $result.read_error = $errorResult
        } finally {
            if ($null -ne $key) { $key.Dispose(); $key = $null }
        }
        try {
            $key = $root.OpenSubKey($subkey, $true)
            if ($null -ne $key) {
                $key.SetValue('Mutable', [string]$configData.attempt)
                $result.write_succeeded = $true
                $result.write_result = 'written'
            }
        } catch {
            $errorResult = Error-Result $_
            $result.write_result = $errorResult.result
            $result.write_error = $errorResult
        }
        $result | ConvertTo-Json -Compress -Depth 4
    } elseif ($Action -eq 'Validate') {
        $key = $root.OpenSubKey($subkey)
        if ($null -eq $key) { throw 'Synthetic registry leaf disappeared' }
        $names = @($key.GetValueNames() | Sort-Object)
        @{ action = $Action; owner_exact = $key.GetValue('OwnerNonce') -ceq [string]$configData.owner_nonce;
            canary_exact = $key.GetValue('Canary') -ceq [string]$configData.canary;
            mutable_exact = $key.GetValue('Mutable') -ceq [string]$configData.mutable;
            private_acl = (Private-Acl $key $sid);
            only_expected_values = (($names -join ',') -ceq 'Canary,Mutable,OwnerNonce' -and $key.SubKeyCount -eq 0)
        } | ConvertTo-Json -Compress
    } elseif ($Action -eq 'Cleanup') {
        $key = $root.OpenSubKey($subkey)
        if ($null -eq $key) {
            @{ action = $Action; removed = $false; already_absent = $true } | ConvertTo-Json -Compress
        } else {
            $names = @($key.GetValueNames() | Sort-Object)
            if ($key.GetValue('OwnerNonce') -cne [string]$configData.owner_nonce -or
                $key.GetValue('Canary') -cne [string]$configData.canary -or
                ($names -join ',') -cne 'Canary,Mutable,OwnerNonce' -or $key.SubKeyCount -ne 0) {
                throw 'Refusing registry cleanup: synthetic leaf ownership changed'
            }
            $key.Dispose(); $key = $null
            $root.DeleteSubKey($subkey, $true)
            @{ action = $Action; removed = $true; already_absent = $false } | ConvertTo-Json -Compress
        }
    }
} finally {
    if ($null -ne $key) { $key.Dispose() }
}
