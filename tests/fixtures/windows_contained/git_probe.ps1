# Runs inside the sandbox: every way a command can try to change repository
# metadata. One line per attempt in results.log: PROBE|name|OK|detail when the
# attempt went through, PROBE|name|ERR|detail when refused. The host compares
# the metadata before and after, so a mistaken OK or ERR here cannot hide a write.
param([string]$Meta = '.git', [string]$RootPath = '')
$ErrorActionPreference = 'Stop'
$log = Join-Path $PWD 'results.log'
function Attempt([string]$name, [scriptblock]$probe) {
    try {
        $detail = ((& $probe 2>&1 | Out-String) -replace '\s+', ' ').Trim()
        Add-Content -LiteralPath $log -Value "PROBE|$name|OK|$detail"
    } catch {
        Add-Content -LiteralPath $log -Value ("PROBE|$name|ERR|" + ($_.Exception.Message -replace '\s+', ' '))
    }
}
function Native([string]$what, [scriptblock]$probe) {
    $output = & $probe 2>&1 | Out-String
    if ($LASTEXITCODE) { throw "$what refused: $output" }
}
$full = Join-Path $RootPath $Meta
$hook = "#!/bin/sh`necho planted"

# ---- plain writes -------------------------------------------------------------
Attempt 'hook_create' { Set-Content -LiteralPath "$Meta\hooks\pre-commit" -Value $hook; 'WROTE' }
Attempt 'config_append' { Add-Content -LiteralPath "$Meta\config" -Value "[core]`n`tfsmonitor = planted.exe"; 'WROTE' }
Attempt 'config_overwrite' { [IO.File]::WriteAllText("$full\config", "[core]`n`tfsmonitor = planted.exe"); 'WROTE' }
Attempt 'head_write' { Set-Content -LiteralPath "$Meta\HEAD" -Value 'ref: refs/heads/planted'; 'WROTE' }
Attempt 'new_file' { Set-Content -LiteralPath "$Meta\planted.txt" -Value 'x'; 'WROTE' }
Attempt 'new_directory' { New-Item -ItemType Directory -Path "$Meta\planted" | Out-Null; 'CREATED' }
Attempt 'info_attributes' { New-Item -ItemType Directory -Force -Path "$Meta\info" | Out-Null; Set-Content -LiteralPath "$Meta\info\attributes" -Value '* filter=planted'; 'WROTE' }

# ---- other names for the same folder ---------------------------------------
Attempt 'upper_case_name' { Add-Content -LiteralPath ($Meta.ToUpper() + '\CONFIG') -Value '[x]'; 'WROTE' }
Attempt 'trailing_dot_name' { Add-Content -LiteralPath "$Meta.\config" -Value '[x]'; 'WROTE' }
Attempt 'short_name' {
    $short = (New-Object -ComObject Scripting.FileSystemObject).GetFolder($full).ShortPath
    [IO.File]::AppendAllText("$short\config", '[x]'); "WROTE through $short"
}
Attempt 'extended_path' { [IO.File]::AppendAllText("\\?\$full\config", '[x]'); 'WROTE' }
Attempt 'device_path' { [IO.File]::AppendAllText("\\.\$full\config", '[x]'); 'WROTE' }
Attempt 'stream_on_file' { Set-Content -LiteralPath "$Meta\config" -Stream planted -Value 'x'; 'WROTE' }
Attempt 'stream_on_folder' { Native 'stream' { cmd /c "echo x> `"$full`":planted" }; 'WROTE' }
Attempt 'substituted_drive' { Native 'subst' { subst Q: $full }; Add-Content -LiteralPath 'Q:\config' -Value '[x]'; 'WROTE' }

# ---- links --------------------------------------------------------------------
Attempt 'junction' { Native 'mklink /J' { cmd /c "mklink /J junction-link `"$full`"" }; Add-Content -LiteralPath 'junction-link\config' -Value '[x]'; 'WROTE' }
Attempt 'junction_powershell' { New-Item -ItemType Junction -Path junction-link-2 -Target $full | Out-Null; Add-Content -LiteralPath 'junction-link-2\config' -Value '[x]'; 'WROTE' }
Attempt 'directory_symlink' { Native 'mklink /D' { cmd /c "mklink /D symlink-dir `"$full`"" }; Add-Content -LiteralPath 'symlink-dir\config' -Value '[x]'; 'WROTE' }
Attempt 'file_symlink' { Native 'mklink' { cmd /c "mklink symlink-file `"$full\config`"" }; Add-Content -LiteralPath 'symlink-file' -Value '[x]'; 'WROTE' }
Attempt 'hardlink' { Native 'mklink /H' { cmd /c "mklink /H hardlink.cfg `"$full\config`"" }; Add-Content -LiteralPath 'hardlink.cfg' -Value '[x]'; 'WROTE' }
Attempt 'hardlink_fsutil' { Native 'fsutil' { fsutil hardlink create hardlink-2.cfg "$full\config" }; Add-Content -LiteralPath 'hardlink-2.cfg' -Value '[x]'; 'WROTE' }
Attempt 'hardlink_powershell' { New-Item -ItemType HardLink -Path hardlink-3.cfg -Target "$full\config" | Out-Null; Add-Content -LiteralPath 'hardlink-3.cfg' -Value '[x]'; 'WROTE' }

# ---- replacing, renaming, deleting ------------------------------------------
Attempt 'replace_by_move' { Set-Content -LiteralPath 'new-config' -Value '[core]'; Move-Item -Force -LiteralPath 'new-config' -Destination "$Meta\config"; 'REPLACED' }
Attempt 'replace_by_python' { Native 'python' { python -c "import os; open('py-config','w').write('[core]'); os.replace('py-config', r'$full\config')" }; 'REPLACED' }
Attempt 'append_by_python' { Native 'python' { python -c "import os; fd=os.open(r'$full\config', os.O_WRONLY|os.O_APPEND); os.write(fd,b'[x]')" }; 'WROTE' }
Attempt 'delete_config' { Remove-Item -Force -LiteralPath "$Meta\config"; 'DELETED' }
Attempt 'delete_hooks' { Remove-Item -Recurse -Force -LiteralPath "$Meta\hooks"; 'DELETED' }
Attempt 'rename_metadata' { Rename-Item -LiteralPath $full -NewName 'renamed-metadata'; 'RENAMED' }
Attempt 'move_metadata' { New-Item -ItemType Directory -Force -Path elsewhere | Out-Null; Move-Item -LiteralPath $full -Destination 'elsewhere\moved-metadata'; 'MOVED' }
Attempt 'delete_metadata' { Remove-Item -Recurse -Force -LiteralPath $full; 'DELETED' }
Attempt 'rename_project_folder' { Rename-Item -LiteralPath $RootPath -NewName 'renamed-project'; 'RENAMED' }
Attempt 'move_project_folder' { Move-Item -LiteralPath $RootPath -Destination ($RootPath + '-moved'); 'MOVED' }

# ---- permissions and attributes ---------------------------------------------
Attempt 'grant_access' { Native 'icacls' { icacls $full /grant "Everyone:(OI)(CI)F" }; 'CHANGED' }
Attempt 'take_ownership' { Native 'takeown' { takeown /f $full /r /d y }; 'CHANGED' }
Attempt 'change_timestamp' { (Get-Item -Force -LiteralPath "$full\config").LastWriteTime = [datetime]'2001-01-01'; 'CHANGED' }

# ---- Git itself, reached without being one literal Git command ----------------
Attempt 'git_config_through_cmd' { Native 'git config' { cmd /c "git config core.fsmonitor planted.exe" }; 'WRITTEN' }
Attempt 'git_commit_through_cmd' { Native 'git commit' { cmd /c "git commit --allow-empty -m probe" }; 'COMMITTED' }
Attempt 'git_update_ref_called' { Native 'git update-ref' { & git update-ref refs/heads/planted HEAD }; 'WRITTEN' }
Attempt 'git_add_through_python' { Native 'git add' { python -c "import subprocess,sys; sys.exit(subprocess.run(['git','add','-A']).returncode)" }; 'WRITTEN' }

# ---- a repository below the project root ---------------------------------------
Attempt 'nested_repository' {
    New-Item -ItemType Directory -Force -Path 'nested\.git\hooks' | Out-Null
    Set-Content -LiteralPath 'nested\.git\hooks\pre-commit' -Value $hook
    Set-Content -LiteralPath 'nested\.git\config' -Value "[core]`n`tfsmonitor = planted.exe"
    'CREATED'
}

# ---- the other protected names, and an ordinary file --------------------------
Attempt 'instruction_file' { Set-Content -LiteralPath 'AGENTS.md' -Value 'planted'; 'WROTE' }
Attempt 'project_file' { Set-Content -LiteralPath 'ordinary.txt' -Value 'x'; 'WROTE' }
