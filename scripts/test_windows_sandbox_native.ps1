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
try {
    . (Join-Path $PSScriptRoot 'windows_test_owner_scope.ps1')
    $ownerScope = [AlysisSandboxTests.DefaultOwnerScope]::EnterCurrentUser()
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/Cargo.toml --locked
    if ($LASTEXITCODE -ne 0) { throw 'Native policy wrapper tests failed.' }
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked
    if ($LASTEXITCODE -ne 0) { throw 'Native primitive tests failed.' }
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked --features account-appcontainer-candidate account_
    if ($LASTEXITCODE -ne 0) { throw 'Disabled account candidate tests failed.' }
    & $Cargo "+$Toolchain" test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked --features account-appcontainer-candidate object_grants
    if ($LASTEXITCODE -ne 0) { throw 'Disabled object grant tests failed.' }
}
finally {
    try {
        if ($null -ne $ownerScope) { $ownerScope.Dispose() }
    }
    finally { Pop-Location }
}
