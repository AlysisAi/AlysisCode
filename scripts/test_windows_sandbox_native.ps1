param(
    [string]$Cargo = 'cargo',
    [string]$Toolchain = '1.90.0'
)

# Only test-process defaults change. Native admission keeps rejecting foreign
# owners and path aliases; synthetic fixtures select their own canonical roots.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
$ownerScope = $null
$originalCargoTarget = $env:CARGO_TARGET_DIR
try {
    . (Join-Path $PSScriptRoot 'windows_test_owner_scope.ps1')
    $ownerScope = [AlysisSandboxTests.DefaultOwnerScope]::EnterCurrentUser()
    # Runner workspaces can grant Authenticated Users write access. Build test
    # executables in a fresh private directory so image-admission tests exercise
    # protected binaries without weakening their native ACL checks.
    $testTarget = Join-Path ([IO.Path]::GetTempPath()) ("alysis-native-tests-" + [guid]::NewGuid())
    $null = New-Item -ItemType Directory -Path $testTarget
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($user)
    foreach ($sid in @($user, [Security.Principal.SecurityIdentifier]'S-1-5-18',
            [Security.Principal.SecurityIdentifier]'S-1-5-32-544')) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $testTarget -AclObject $acl
    $env:CARGO_TARGET_DIR = $testTarget
    Write-Host "Native test artifacts: $testTarget"
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/Cargo.toml --locked
    if ($LASTEXITCODE -ne 0) { throw 'Native policy wrapper tests failed.' }
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked
    if ($LASTEXITCODE -ne 0) { throw 'Native primitive tests failed.' }
    # Handoff tests assert that consumed raw handle values are closed. Windows
    # can reuse those values in parallel tests, so run this complete family in
    # a separate single-threaded process. Every test still runs; bundled native
    # source remains byte-for-byte bound to the shipped helper's source receipt.
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked --features account-appcontainer-candidate account_ -- --skip account_handoff_
    if ($LASTEXITCODE -ne 0) { throw 'Disabled account candidate tests failed.' }
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked --features account-appcontainer-candidate account_handoff_ -- --test-threads=1
    if ($LASTEXITCODE -ne 0) { throw 'Account handoff tests failed.' }
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked --features account-appcontainer-candidate object_grants
    if ($LASTEXITCODE -ne 0) { throw 'Disabled object grant tests failed.' }
}
finally {
    try {
        if ($null -ne $ownerScope) { $ownerScope.Dispose() }
    }
    finally {
        $env:CARGO_TARGET_DIR = $originalCargoTarget
        Pop-Location
    }
}
