# Native cryptography wheels are unavailable for Windows ARM64. Build the pinned
# upstream LTS OpenSSL source for static linking into the locked Python package.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:RUNNER_OS -ne 'Windows' -or $env:RUNNER_ARCH -ne 'ARM64' -or
    [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    throw 'This build requires the native Windows ARM64 hosted runner.'
}
$opensslVersion = '3.5.9'
$opensslSha256 = '603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a'
$opensslUrl = "https://github.com/openssl/openssl/releases/download/openssl-$opensslVersion/openssl-$opensslVersion.tar.gz"
$opensslArchive = Join-Path $env:RUNNER_TEMP "openssl-$opensslVersion.tar.gz"
$opensslSource = Join-Path $env:RUNNER_TEMP "openssl-$opensslVersion"
$opensslPrefix = Join-Path $env:RUNNER_TEMP 'alysis-openssl-arm64'
Invoke-WebRequest -Uri $opensslUrl -OutFile $opensslArchive
if ((Get-FileHash -LiteralPath $opensslArchive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $opensslSha256) {
    throw 'OpenSSL source checksum mismatch.'
}
& tar -xzf $opensslArchive -C $env:RUNNER_TEMP
if ($LASTEXITCODE -ne 0) { throw 'OpenSSL source extraction failed.' }
$vswherePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$vsInstall = (& $vswherePath -latest -products '*' -property installationPath).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($vsInstall)) {
    throw 'Visual Studio native compiler was not found.'
}
Import-Module (Join-Path $vsInstall 'Common7/Tools/Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vsInstall -SkipAutomaticLocation -DevCmdArguments '-arch=arm64 -host_arch=arm64'
$perlPlatform = & perl -e 'print $^O'
if ($LASTEXITCODE -ne 0 -or $perlPlatform -cne 'MSWin32') {
    throw 'OpenSSL requires native Windows Perl, not an MSYS/Cygwin interpreter.'
}
Push-Location $opensslSource
try {
    & perl Configure VC-WIN64-ARM no-shared no-module no-makedepend "--prefix=$opensslPrefix" '--libdir=lib'
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSL configuration failed.' }
    & nmake
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSL compilation failed.' }
    # Upstream supports parallel recipes; retain the complete native test suite.
    & nmake HARNESS_JOBS=4 test
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSL native tests failed.' }
    & nmake install_sw
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSL installation failed.' }
} finally {
    Pop-Location
}
foreach ($relativePath in @('lib/libcrypto.lib', 'lib/libssl.lib', 'include/openssl/ssl.h')) {
    if (-not (Test-Path -LiteralPath (Join-Path $opensslPrefix $relativePath) -PathType Leaf)) {
        throw "Missing native OpenSSL build output: $relativePath"
    }
}
Write-Output "Built and tested OpenSSL $opensslVersion for native Windows ARM64 ($opensslSha256)."
