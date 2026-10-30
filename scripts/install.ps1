# Alysis Code installer for Windows PowerShell 5.1 and PowerShell 7.
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.ps1 | iex
#
# Installs uv if needed, then the CLI and an isolated Python runtime.
# No administrator access, system Python, or pipx is required.
# ALYSIS_VERSION=<version> pins a release; ALYSIS_PYTHON defaults to 3.12.
# ALYSIS_NO_MODIFY_PATH=1 skips persistent PATH changes (the current shell is updated).

& {
    $ErrorActionPreference = 'Stop'
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Use scripts/install.sh on Linux, macOS, or inside WSL.'
    }

    function Find-Uv {
        $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($command) { return $command.Source }
        $directories = @($env:UV_UNMANAGED_INSTALL, $env:UV_INSTALL_DIR, $env:XDG_BIN_HOME)
        if ($env:XDG_DATA_HOME) { $directories += Join-Path $env:XDG_DATA_HOME '../bin' }
        $directories += Join-Path $env:USERPROFILE '.local/bin'
        $directories += Join-Path $env:USERPROFILE '.cargo/bin'
        foreach ($directory in $directories) {
            if ($directory) {
                $candidate = Join-Path $directory 'uv.exe'
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    return (Resolve-Path -LiteralPath $candidate).Path
                }
            }
        }
    }

    function Invoke-Uv {
        & $uv @args
        if ($LASTEXITCODE -ne 0) { throw "uv failed (exit code $LASTEXITCODE). See the output above." }
    }

    function Test-PathEntry([string]$Value, [string]$Entry) {
        foreach ($part in ($Value -split ';')) {
            if ([Environment]::ExpandEnvironmentVariables($part.Trim().Trim('"')).TrimEnd('\', '/') -ieq
                $Entry.TrimEnd('\', '/')) { return $true }
        }
        return $false
    }

    $uv = Find-Uv
    if (-not $uv) {
        Write-Host 'Installing uv...'
        # Keep uv's bootstrap from changing PATH; add the necessary directories
        # once below, preserving the existing user PATH.
        $savedNoModifyPath = $env:UV_NO_MODIFY_PATH
        $savedTls = [Net.ServicePointManager]::SecurityProtocol
        try {
            $env:UV_NO_MODIFY_PATH = '1'
            [Net.ServicePointManager]::SecurityProtocol = $savedTls -bor [Net.SecurityProtocolType]::Tls12
            $installer = (Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1').Content
            if ($installer -is [byte[]]) { $installer = [Text.Encoding]::UTF8.GetString($installer) }
            & ([scriptblock]::Create($installer))
        } finally {
            $env:UV_NO_MODIFY_PATH = $savedNoModifyPath
            [Net.ServicePointManager]::SecurityProtocol = $savedTls
        }
        $uv = Find-Uv
        if (-not $uv) { throw 'uv installation did not produce uv.exe. Check the installer output and retry.' }
    }

    $binDirectory = (Invoke-Uv tool dir --bin | Out-String).Trim()
    if (-not $binDirectory) { throw 'uv did not report a tool executable directory.' }
    $tools = Invoke-Uv tool list | Out-String
    $managed = $tools -match '(?m)^alysis-code\s'
    $entryPoints = @('alysis.exe', 'alysis-code.exe', 'alysis-ide-bridge.exe')
    if (-not $managed) {
        foreach ($name in $entryPoints) {
            $destination = Join-Path $binDirectory $name
            if (Test-Path -LiteralPath $destination) {
                throw "$destination already exists. Upgrade that installation with 'alysis update', or remove it with its original installer (for example 'pipx uninstall alysis-code') before retrying."
            }
        }
    }

    $pythonVersion = if ($env:ALYSIS_PYTHON) { $env:ALYSIS_PYTHON } else { '3.12' }
    $package = 'alysis-code'
    if ($env:ALYSIS_VERSION) {
        $version = $env:ALYSIS_VERSION.Trim().TrimStart('v')
        if ($version -notmatch '^\d+\.\d+\.\d+([a-zA-Z0-9.+-]*)$') {
            throw 'ALYSIS_VERSION must be a package version such as 0.15.2.'
        }
        $package += "==$version"
    }
    Write-Host 'Installing Alysis Code (Python is downloaded automatically if needed)...'
    # Copy keeps shared uv cache hardlinks outside the contained worker's read roots.
    $installArguments = @('tool', 'install', '--python', $pythonVersion, '--link-mode', 'copy', $package)
    if ($managed) { $installArguments += @('--reinstall', '--upgrade') }
    Invoke-Uv @installArguments

    $alysis = Join-Path $binDirectory 'alysis.exe'
    if (-not (Test-Path -LiteralPath $alysis -PathType Leaf)) {
        throw "Installation did not produce $alysis."
    }
    $installedVersion = & $alysis --version
    if ($LASTEXITCODE -ne 0) { throw "The installed CLI could not start: $alysis" }

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $newUserPath = $userPath
    foreach ($directory in @((Split-Path -Parent $uv), $binDirectory) | Select-Object -Unique) {
        if (-not (Test-PathEntry $env:Path $directory)) {
            $env:Path = "$directory;$env:Path"
        }
        if (-not (Test-PathEntry $newUserPath $directory)) {
            $newUserPath = "$directory;$newUserPath"
        }
    }
    if ($env:ALYSIS_NO_MODIFY_PATH -ne '1' -and $newUserPath -ne $userPath) {
        [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    }
    $resolved = Get-Command alysis -ErrorAction SilentlyContinue
    if (-not $resolved -or $resolved.Source -ine $alysis) {
        Write-Warning "Another command shadows this installation. Run it directly: & '$alysis'"
    }
    Write-Host ""
    Write-Host "Alysis Code $installedVersion is installed."
    Write-Host "Run 'alysis login' in this terminal, then approve the code in your browser."
    Write-Host "After sign-in, open your project folder and run 'alysis'."
    Write-Host "On Windows x64, check the bundled native sandbox with 'alysis sandbox doctor'."
    if ($env:ALYSIS_NO_MODIFY_PATH -eq '1') {
        Write-Host "Persistent PATH was left unchanged. Executables are in $binDirectory."
    } else {
        Write-Host 'Other open terminals may need to be restarted to pick up the PATH change.'
    }
}
