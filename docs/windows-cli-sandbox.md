# Standalone Windows CLI sandbox (beta)

The native Windows x64 CLI includes the signed Windows sandbox helper. The
Desktop app, Docker, a separate helper download, an administrator account and
manual sandbox environment variables are not required. With the normal default
`mode=strict, backend=auto`, Windows selects `windows-mxc`. A missing, damaged or
unsupported helper stops command execution; it never silently selects the host
or Docker. Explicit Full access / `mode=off` and explicitly selected backends
remain user choices. WSL continues to use the Linux backend.

## Install and check

Use a package that includes the native Windows sandbox helper. With uv installed:

```powershell
uv tool install --python 3.12 --link-mode copy alysis-code
alysis sandbox doctor
```

`scripts/install.ps1` performs that installation locally. An existing uv
installation needs `uv tool install --reinstall --link-mode copy alysis-code`.
pip and pipx installations copy their files normally. The copy option matters:
shared package-cache hardlinks cannot be granted to a contained file worker.
The CLI retains that boundary check and reports a diagnostic failure for such an
installation. This is an installation option, not a sandbox activation setting.

The one-file Windows executable also contains the same pinned helper and uses a
fixed contained file-worker entry point. It does not need an external Python or
Desktop installation. `sandbox doctor` checks the helper, runs a real command,
tests outside read/write denials and exercises the contained file worker.
`sandbox doctor --no-smoke` only checks helper identity and host capability.

The current helper requires Windows x64 with MXC/PSEC support and local fixed
NTFS workspace paths. Unsupported versions, ARM64, UNC paths, reparse points,
shared hardlinks and a project inside the engine's trusted runtime are refused.
Network access retains the engine's existing explicit off/on policy. Model API
requests run in the trusted engine, separately from sandboxed shell commands.
Python-based installations expose their base Python installation read-only as
a fallback for the `python` command inside the Windows sandbox. Existing
admitted commands retain PATH precedence. The engine virtualenv is not put on
PATH, user site-packages and Python startup environment are not inherited, and
the fallback cannot modify its installation. Frozen executables do not provide
this general-purpose interpreter. The folders on the person's PATH are readable to a
command (see "The person's programs" below). A tool installed anywhere else
needs `windows_read_roots` or a folder request. Nothing of the home directory
is granted. Durable services remain unavailable with this backend; use
session-owned background commands.

## What a command finds around it

### A local application data and a temp folder of its own

Every command that may write gets a scratch folder, as a macOS command does:
`%LOCALAPPDATA%\alysis-code\scratch\<random name>`. It holds a home laid out
the way Windows lays one out, and the variables for the places programs
write point into it:

| Variable | Where it points |
| --- | --- |
| `LOCALAPPDATA` | `home\AppData\Local` in the scratch folder |
| `TEMP`, `TMP`, `TMPDIR` | `home\AppData\Local\Temp` in the scratch folder |
| `USERPROFILE`, `HOMEDRIVE` + `HOMEPATH` (PowerShell's `$HOME` and `~`), `APPDATA` | The person's real profile and roaming application data, readable and never writable: see "The person's home and installed programs". When those cannot be used, `home` and `home\AppData\Roaming` in the scratch folder |

The folder is the only place outside the approved folders the command can
write. It is deleted when the command ends; a background command keeps its
folder until it is stopped. A lock file beside each folder, held open by the
engine process that made it, tells a folder in use from one a crash left
behind, and the next engine process that runs a command deletes those.
Nothing in the folder is read back as trusted, and it is not reserved or
protected the way a project is.

Because the folder goes with the command, nothing a command puts there is
there for the next one: the download caches of npm, pip and uv, Go's build
cache. What a tool installs into the project stays.

Programs that ask Windows for the local application data instead of reading
the variable get the app container's own folder, `Packages\<container>\AC`
below the profile's local data. With the person's real profile, Windows has
made that folder for the command and removes it afterwards. With the scratch
home it does not exist until someone makes it: the API then answers with
nothing, and a program builds a path relative to the working directory. In a
live run PowerShell left its module cache in two projects as
`Microsoft\Windows\PowerShell`. The engine therefore has the API create that
folder, and the one for the internet cache, before the command starts.
Creating them any other way does not change the answer the process was
already given.

Before this change the temp folder was the project folder, so temporary
files, caches and "throwaway" environments landed among the person's files,
and there was no home at all: `$HOME` was empty, `~` did not resolve and
Python's `Path.home()` raised.

A command that cannot write (read-only access) and the engine's own fixed
workers (contained Git, the file workers) get no scratch folder. They keep
the working directory as their temp folder, as before.

If the scratch folder cannot be prepared (no `LOCALAPPDATA`, or it lies
behind a link or on a volume the sandbox refuses), the command runs with the
previous arrangement instead of failing. The folder is a convenience, not
part of the boundary.

### The person's programs

A command can run the programs on the person's PATH, as it can on macOS and
Linux. Before this change a Windows command saw Node.js from Program Files,
the engine's own Git and a fallback Python, and nothing else: the person's
Python with its packages, uv, the GitHub CLI and ripgrep were "not
recognized", and a project's own virtual environment could not start because
the Python it was made from was unreadable.

Each PATH folder is made readable, never writable, and is put on the
command's PATH in the person's order, after the engine's own Node.js, Git and
system folders and before its fallback Python. So `python` is the person's
Python when one is on the PATH, and the engine's when none is.

| On the PATH | What the command can read |
| --- | --- |
| A folder under Program Files (`C:\Program Files\Git\cmd`) | The whole installation (`C:\Program Files\Git`): a program there needs the files beside its `bin` or `cmd` folder, and nothing personal is kept there |
| Any other folder (`%LOCALAPPDATA%\Programs\Python\Python314`, `%USERPROFILE%\.local\bin`) | That folder |

Left out, without failing the command:

- an entry that is not an existing folder on a local drive, a relative entry,
  and a folder reached through a link;
- a folder that contains or lies inside an approved folder, the scratch
  folder, the Windows directory (its runtime folders are readable already),
  the application's settings and data, or the engine's own runtime: a program
  from the engine's virtual environment would run with the engine's packages;
- a folder that contains the person's home or application data;
- a folder that holds links instead of programs (the Store's app aliases in
  `WindowsApps`): the sandbox cannot start them;
- a folder that offers `python` without being a Python installation, such as
  the `Scripts` folder of a virtual environment or the shim folder of a
  version manager. Its `python` starts a Python somewhere else, which is
  unreadable, and it would be found before one that works.

The engine's fixed workers and read-only commands do not get these folders.

A PATH folder is not searched for links before it is granted. The check that
does this for a project walks every file, and for a Python installation with
23,000 files that took six seconds per command. What the sandbox does with a
link was tested instead (`tests/test_windows_mxc.py`): the content behind a
junction is refused. A file in the folder that is a hard link is readable,
because that name is in the folder, and it cannot be written. Symbolic links
could not be created on the probe machine and were not tested. Granting
eighteen folders cost about 0.1 seconds per command on that machine.

Known gaps, measured on the probe machine:

- **Git Bash and WSL.** `bash` from Git for Windows is found and exits with
  code 1 and no output: its runtime cannot start in this sandbox. `wsl.exe`
  is refused.
- **The `py` launcher** finds no installed Python. `python` works.
- A tool that keeps its real program in a folder that is neither on the
  PATH nor readable under "The person's home and installed programs" is
  found and fails to start: a version manager that keeps its versions in the
  local application data, a toolchain on another drive.

### The person's home and installed programs

A language toolchain is more than the program on the PATH. rustup's `cargo`
finds its compiler in `~\.rustup` and the crates it has downloaded in
`~\.cargo`; uv finds its Pythons in the roaming application data; `rustc`
finds the linker and the Windows SDK in Program Files. With only the PATH
folders readable and the home replaced by an empty scratch folder, every
tool of that kind was found and then failed. Measured in the real sandbox on
the probe machine, with every toolchain installed there:

| Inside the sandbox | Before | Now |
| --- | --- | --- |
| Python: run tests, make a virtual environment, `pip install` into it | Works | Works |
| Python packages installed with `pip install --user` | Not looked for | Looked for |
| uv: make an environment with one of uv's own Pythons, `uv pip install` | "Failed to inspect Python interpreter" | Works |
| Node.js: `npm test`, `npm install` | Works | Works |
| `npm ls -g`, the globally installed packages | Error (`ENOENT`) | Listed |
| Rust from rustup: `cargo --version`, `rustc --version` | "no default is configured" | Works |
| `cargo build`, `cargo run`, `cargo test`, MSVC and GNU toolchain | Fails | Works; a check takes 0.2 s |
| `cargo build --offline` with a dependency the person downloaded before | Fails | Works |

In the desktop app, with the fast model and "Approve for me", the task "the
tests of this Rust crate fail, fix them" completed in 102 seconds without a
card: `cargo test`, the edit, `cargo test` again.

No tool is named in the engine for this. A command is told where the
person's home and roaming application data really are, and can read what
programs keep there, never write it. Codex does the same (it leaves the
whole disk readable and the environment as it is), and so does Windows for
a sandboxed app: the profile stays where it is and the app gets a local
application data and a temp folder of its own.

Readable, never writable:

| Where | What |
| --- | --- |
| The profile | Every folder whose name starts with a dot (`.cargo`, `.rustup`, `.m2`, `.gradle`, `.nuget`, `.cache`); a folder without a dot when it is on the PATH or holds the `bin`, `cmd`, `shims` or `condabin` folder that is (`go`, `scoop`); the files whose names start with a dot (`.gitconfig`) |
| The roaming application data, ProgramData | A folder that is a tool's. A program on the PATH lives in it, or it is named like a program on the PATH, or like a tool folder in the profile (`.nuget` and `NuGet`). "Named like" is the whole name (`uv`, `Python`), the name up to a separator (`npm-cache`), or the end of a name without separators when that end has five letters or more (`pypoetry`). In ProgramData also `Microsoft\VisualStudio`, where the Visual Studio installer records what is installed and compilers ask for the linker and the SDK |
| Program Files, Program Files (x86) | Everything the person can read there: compilers, SDKs and runtimes that are not on the PATH |

A PATH folder the command itself can write is no evidence for any of this.
A command could put a file there with the name of the folder it wants to
read. Windows' own folders are no evidence either.

Left unreadable, in every one of those places:

- **Credential folders and files, by name.** The twelve folders Codex leaves
  out of the profile (`.ssh`, `.tsh`, `.brev`, `.gnupg`, `.aws`, `.azure`,
  `.kube`, `.docker`, `.config`, `.npm`, `.pki`, `.terraform.d`), this
  engine's own, `.mcp-auth`, and `Microsoft`, `gnupg`, `gh`, `GitHub CLI`
  and `gcloud` in the roaming data. Directly inside a readable folder, every
  name the engine already treats as a credential (`.npmrc`, `.netrc`,
  `.git-credentials`, `.env`, key files) and every name that contains
  `credential`, `secret`, `password`, `token` or `history` is refused on its
  own: `~\.cargo\credentials.toml` stays unreadable while the rest of
  `~\.cargo` is read. Up to three folders further down, a file with the
  exact name of a credential is refused (`auth.json`, `credentials.json`,
  `token`); names are not judged by a word there, because a package cache
  holds thousands of folders called things like `js-tokens`. In a PATH
  folder a program is not refused for its name (`secretlint.cmd`).
- **The homes of other coding agents** (`.claude`, `.codex`, `.gemini`,
  `.copilot`, `.cursor`, `.continue`, `.codeium`, `.windsurf`, `.aider`,
  `.openai`, `.anthropic`, `.opencode`, and what starts with those names; in
  the roaming data and ProgramData the same names without the dot): they
  hold sign-ins and what was said to the agent.
- **Browser profiles, and apps built on a browser engine** (chat and mail
  clients, editors). Each keeps cookies and saved sign-ins, and a command in
  this sandbox can decrypt what Windows protected for the user. A profile is
  recognised by what it holds (`Local State`, `Cookies`, `Login Data`,
  `profiles.ini`, `logins.json`, `key4.db`). A folder that holds one
  directly is not readable; one found up to three folders down is refused
  where it lies, so `~\scoop` is read without `~\scoop\persist\firefox`.
- **The keys of a program that is running on the computer**: Jupyter's
  `runtime` folder and IPython's `security` folder hold, for every running
  kernel, the port and the key that let a program give it code to run.
- **The person's own folders**: Desktop, Documents, Downloads, Pictures,
  Music, Videos, OneDrive, Dropbox and the like, and every other folder
  without a dot that no PATH program lives in. A folder of projects stays
  unreadable.
- **Everything else in the roaming application data and in ProgramData.**
  Messengers, remote-desktop tools and games keep sessions there and nothing
  in the folder says so, which is why a folder there needs evidence that it
  is a tool's. On the probe machine five of forty roaming folders were
  readable.
- **The local application data**, apart from the PATH folders in it. The
  command has its own.
- **A folder that lies inside a project, the scratch folder, anything the
  request denies or anything of the application's own.** A tool's folder
  that holds one of those is read beside it, one level down: with a project
  in `~\go\src`, `~\go\bin` and `~\go\pkg` are readable and `~\go\src`
  is not; with a write request granted for `~\.cargo\registry`, the rest
  of `~\.cargo` stays readable.
- Links, a folder Windows spells differently from its path, a folder with
  more than 4,096 entries, and a folder with a credential that cannot be
  named to the helper.

What a person approved by name for a command stays readable: a credential
file given with `permissions.read_files` is not refused for its name.

Folder names and sizes outside these places could be listed before this
change and still can; that is the directory discovery the sandbox always
had, and it opens no file.

What this costs and what it does not solve:

- Deciding the list took 0.11 seconds for the first command of an engine
  process and 0.01 seconds after that on the probe machine. What a folder
  holds directly is remembered until its modification time changes. What
  lies further down is looked for in at most 300 folders per readable
  folder and remembered for fifteen minutes, so a browser profile that
  appears deep inside a tool's folder is refused that much later, and one
  beyond the 300 folders is not found.
- **Credentials under other names, further down.** Only the names above are
  refused. A build tool's settings file that can also hold a password stays
  readable, because the tool stops without it: Maven's `settings.xml`,
  `gradle.properties`, `NuGet.Config`, `pip.ini`. So does a token a tool
  keeps under a name of its own several folders down. A credential that
  appears while a background command runs is not refused for that command.
- **Writing in the home is refused.** A global install (`npm install -g`,
  `pip install --user`, `cargo install`), `git config --global`, and a
  package cache in the home when something has to be added to it: `cargo`
  with a dependency that is not downloaded yet, `go mod download`, Maven,
  Gradle and NuGet restores. The command needs `permissions.write_folders`
  for that one folder, which the access review decides. A cache is not
  writable by default because a command that can change it changes what a
  later build on the host compiles. Not tried against a real cache.
- **`cargo` needs `permissions.local_preview` together with
  `permissions.network`.** Without it it reports "Could not resolve
  hostname (getaddrinfo() thread failed to start)": its resolver connects
  to itself on 127.0.0.1. With both it downloaded a crate and then stopped
  at the unwritable registry. `curl.exe`, pip and npm need the network only.
- **A tool that finds its installed parts through the local application
  data does not find them**, because the command has its own: corepack's
  pnpm and yarn, Volta, Playwright's browsers, the Android SDK in its
  default place. Point the tool at the real folder with its own setting and
  request `permissions.read_folders` for it.
- **A variable the person saved to point a tool at its files** (`JAVA_HOME`,
  `GOPATH`, `ANDROID_HOME`) is not passed on. The environment is still a
  fixed list of names. A tool that needs one has to be given it in the
  command.
- **An unreadable file a tool expects** can stop it where a missing one
  would not. npm was tried with an unreadable settings file and carries on.
  Git inside a command prints `warning: unable to access
  '.../.config/git/ignore': Permission denied` when the person has that
  file, and carries on with exit code 0.
- **`.gitconfig` is readable**, so Git inside a script commits with the
  person's identity and settings. It was unreadable before. With
  `commit.gpgsign` set there, a commit made inside a command fails: the key
  stores are not readable.
- **A listed file that is deleted in the instant between the decision and
  the start of the command** fails that command with the helper's
  "existing, inspectable policy paths" error. Lock and temp files in the
  profile are not listed for that reason.
- **Not installed on the probe machine, so not tried:** Go, Java, .NET,
  Ruby, PHP, conda. Their state lies in places the rules above cover
  (`~\go` when `~\go\bin` is on the PATH, `~\.m2`, `~\.gradle`,
  `~\.nuget` and `NuGet` in the roaming data, Program Files). Whether each
  works is not known.
- The `py` launcher still finds no Python, and pnpm and yarn from corepack
  still fail; on the probe machine pnpm fails outside the sandbox as well.

A check that failed and its rerun are compared as the same command whatever
the home holds in between: which home is readable counts, not the list of
folders in it.

A command that cannot write, and the engine's fixed workers, get none of
this, like the PATH folders. If the profile cannot be used, the command gets
the scratch home as before: when it lies behind a link, when `USERPROFILE`
is not set, and when `USERPROFILE` names a folder that is not the profile
Windows has for the person. Inside the sandbox the Windows folder API
answers from that variable, and only below the person's own profile has
Windows made the folder for the command's local data; anywhere else a
program that asks the API gets nothing and writes into the project. Roaming
application data that cannot be used, or that names the profile or a folder
above it, is left out alone.

### Ordinary folders and everyday commands

The sandbox is used for more than code: counting, copying and zipping the
person's files, converting an image, answering a question about the
computer. A survey of such commands in the real sandbox found three causes
that had nothing to do with any tool or language. The same survey after the
changes:

| Inside the sandbox | Before | Now |
| --- | --- | --- |
| Any command in a project that holds a hard-linked file (a uv environment, pnpm's `node_modules`, a file with two names) | Refused before it starts: "cannot grant a tree containing hardlinked files" | Runs |
| Any command in a project that holds a link (pnpm's `node_modules`, a folder linked from elsewhere) | Refused before it starts: "cannot grant a tree containing reparse points" | Runs; what lies behind a link out of the project is refused |
| Read or write the person's Documents after it was granted (it holds the links `My Music`, `My Pictures`, `My Videos` that Windows puts there) | Refused, same message | Works; what lies behind the links is refused |
| Count the entries of a folder granted for writing | 13 for a folder with 2 | 2 |
| `Copy-Item` a granted folder into another | Error: "An item with the specified name ... .agents already exists" | Copies |
| `Compress-Archive` a granted folder | Error: "AGENTS.md ... is being used by another process", and an empty `.github/skills` folder in the zip | The person's files only |
| `robocopy` a granted folder | Eleven empty entries left in the destination for good | The person's files only |
| `git init` in a new folder inside a granted folder | Fails | Works |
| `$PWD` | `Alysis:\`, which .NET refuses: "The given path's format is not supported" | The real path |
| Save an image with `System.Drawing`, send a file to the recycle bin, `[IO.File]` with `$PWD` | Fails with that message | Works |
| The folder above the working folder (`Split-Path $PWD -Parent`) | The working folder itself | The folder above |
| A project with 20,000 files | Every file inspected before every command | Nothing inspected |

**Folders with links in them.** Before a folder was granted, every file in
it was looked at, and a hard-linked file or a link anywhere inside refused
the folder. The legacy helper needs that: it grants access by changing
permissions, and a hard-linked file has one set of permissions for all its
names. This sandbox decides by path and changes no permissions, and Codex
has no such rule. The folder is granted as it is. Two things follow, both
tested in the real sandbox:

- What lies behind a link is decided where it lies. A link in the project
  that leads out of it opens nothing, for reading or writing; one that leads
  to the project's own `.git` does not make `.git` writable.
- A file in the project that has another name somewhere else can be read and
  changed through the project, and the other name changes with it. That is
  how uv and pnpm share files between a cache and a project, and a command
  that edits an installed package in such a project edits the cache. A
  command cannot make such a second name itself for a file it may not write.
  One case is still refused, with a message that says what to do: an
  instruction file (`AGENTS.md`, `CLAUDE.md`, `CONVENTIONS.md`) that has a
  second name, because the command could change it through the name that is
  not protected.

**A folder granted for one command is not a project.** Names are reserved
in a project so that a command cannot leave instructions or a repository
there for the agent or for Git to trust later. A folder a command is let
write with `permissions.write_folders` was treated the same, and the eleven
reserved entries appeared among the person's files for as long as the
command ran. In such a folder nothing is reserved now. What exists of those
names there stays read-only; what does not exist can be created, as in
Codex, and stays: a command can make an `AGENTS.md` or a `.git` in a folder
the person let it write. The project itself and the folders attached to the
session keep every reserved name.

**The command's location is the folder itself.** PowerShell was started on
a drive named `Alysis:` that stood for the project, because the legacy
sandbox cannot see the folders above. This sandbox can, so the command now
stands in the real folder. Two cases keep the drive: the engine's fixed
workers, which see nothing above their folders, and a folder the
application manages inside its own data. That data folder is hidden from
the command, PowerShell cannot stand in a folder whose parents it cannot
see, and a program started there found itself in the root of the drive. In
such a folder `$PWD` is still `Alysis:\`; `(Get-Location).ProviderPath`
gives the real path.

Still not reachable from the sandbox, measured, and the model is told so:

| Attempt | Result |
| --- | --- |
| Read the clipboard | Refused |
| Word or Excel through COM (`New-Object -ComObject Word.Application`) | Refused (`80070005`) |
| `Get-Service`, `Get-CimInstance`, `tasklist` | Refused |
| Open a web page in the browser | Refused |
| Free disk space with `Get-PSDrive` | Empty values; `[System.IO.DriveInfo]` answers |
| `winget` | Not found: the folder of Store app aliases is not put on the PATH |
| Text to speech (`System.Speech`) | Fails |
| Zip and unzip, CSV, file hashes, date and time zone, the registry's list of installed programs, `whoami`, `ipconfig`, a web request with `permissions.network` | Work |

There is no way yet to run one such command outside the sandbox on request,
as Codex's "escalate" does. A task that needs the clipboard, an Office
program or a service cannot be done by a sandboxed command.

### What it is told about the computer

The environment is still built from a fixed list of names and never copied
from the engine. The list now includes what every Windows process is told
about the computer: `SystemDrive`, `ProgramFiles`, `ProgramFiles(x86)`,
`ProgramW6432`, the three `CommonProgramFiles` variables, `ALLUSERSPROFILE`,
`PUBLIC`, `USERNAME`, `COMPUTERNAME`, `NUMBER_OF_PROCESSORS`, the four
`PROCESSOR_*` variables and `OS`. Without them `$env:SystemDrive\Users`
resolved inside the project and PowerShell's module path contained a literal
`%ProgramFiles%`. None of them is a credential or a location in the person's
profile.

### Names the sandbox reserves in the project

The sandbox can protect only a name that exists. To keep a command from
creating `AGENTS.md`, a `.git` folder or a skill folder that the engine or
Git would later trust, an empty entry is made for every protected name the
project does not have: `AGENTS.md`, `CLAUDE.md`, `CONVENTIONS.md`, `.git`,
`.alysis`, `.sylliptor`, `.codex`, `.claude`, `.agents`, `.alysis_skills`
and `.github\skills`. A command sees them in a directory listing. Only
`.github\skills` is reserved, not `.github`; when the project has no
`.github`, an empty one appears as the folder above it.

After an ordinary command they are removed again. A background command keeps
them for as long as it runs. They are removed when the last background
command in the folder has stopped; if another command is running there at
that moment, when that command ends. This holds for the project, for the
folders approved for the command and for the folders attached to the
session. They used to stay for good: in a live run of the desktop app, a
task that started a development server left all eleven empty entries in the
person's project.

Removing them needs to know that no other command still relies on them, in
this engine process or in another one. So each background command leaves a
record outside the project, under `%LOCALAPPDATA%\alysis-code\reservations`:
which entries it made, each with the identity it was created with. The
command holds its record open while it runs. Windows refuses to rename a
file that is held open, so a record that can be renamed belongs to a command
that has ended or to an engine process that is gone. When no record of a
folder is held any more, every recorded entry is removed that is still the
empty object that was made. The records are looked at when a background
command stops, and before and after every command, each time under the
folder's lease, the mutex a command takes before it makes or removes
entries.

The records are not what protects a running command. The helper has every
reserved entry open for as long as its command runs, and an entry that is
open that way cannot be deleted. A removal that comes too early fails on
that and is tried again later.

What is never removed: an entry the person already had, one that was
replaced, written to or made read-only meanwhile (an `AGENTS.md` with
content, a `.git` that became a repository), and a folder that holds
anything not made here. An entry that something keeps from being deleted is
recorded again and tried by the next command, together with the folder that
holds it. So are the entries of an engine process that ended without
stopping its command.

A removed name is free at once, also while another program still has the
entry open (a scanner, an indexer): the next command makes the entry anew
under the same name and would fail otherwise. Windows versions that cannot
do that keep the name until the other program lets go.

The engine's own Git job takes leftover entries back before it looks at the
folder, so that `git add -A` does not stage empty files the engine deletes
afterwards. The person's own `git add .` during a command does stage the
empty reserved files; `git status` lists them as untracked while a command
runs. A reserved file that Git has staged, or tracks as the empty file it
is, is the person's from then on and is not taken back; the repository may
lie above the project, as in a repository of several projects. A tracked
file with content that the person deleted is not such a file: the empty
entry that took its name is taken back, and Git shows the deletion as the
person left it.

Three cases still leave entries behind, all empty and safe to delete by
hand: entries made by a version before this one, of which there is no
record; a background command started while another command kept the folder
busy for more than fifteen seconds, which then starts without the lease and
without a record, as before; and an engine process that dies in the moment
between making the entries and writing the record.

Removing or making an entry while another command is being watched for the
files it changes is not counted as a change by that command, as long as the
same engine process did it. Only the exact file is excused: a command that
deletes the person's own empty `AGENTS.md`, or writes into a reserved one, is
still noticed. When another engine process takes the entries back during
that time, the watched command is charged with deleting them.

### Git keeps working while a command runs

While a command ran in a project, every `git add`, `git commit` and
`git checkout` in that repository failed with `fatal: unable to write new
index file`: for the engine's own Git job and for the person's Git in a
terminal or an editor alike, during a foreground command and for as long as
a background command ran, a development server for instance.

The helper holds every protected folder open while its command runs, with a
handle that shares reading only. For a folder that also stops every rename
*into* it, by any program, and Git replaces its index, `HEAD` and its
configuration by renaming a lock file into `.git`.

A repository is therefore given to the helper as a read-only rule, without
the request to hold it open: the `.git` folder of a project, and the
repository a `.git` file points to inside the project. Two things had to
hold for that.

- **The rule alone must refuse the command.** The containment probes were
  run with the repository not held, for all four repository layouts: direct
  writes, other spellings, alternate data streams, junctions and links, a
  substituted drive, renaming or deleting the folder or its parent,
  `icacls`, `takeown`, and Git reached through `cmd`, the call operator and
  Python. Everything was refused and the host found the repository
  byte-identical, as with the handle.
- **The folder must not go away under the rule.** The rule names a path. If
  the repository were deleted or renamed while a command runs, the command
  could make its own `.git` under that name. The helper's handle prevented
  that for everybody. The engine now holds the repository itself, with a
  handle that does not share deleting and does share writing: nobody can
  delete or rename `.git` while a command runs there, as before, and
  everybody but the command can write into it. A foreground command's
  reservations hold it; a background command holds it until it is stopped.
  The helper is spared only a repository the engine does hold: one that
  cannot be opened, or that appears between the two steps, is given to the
  helper to hold as before, and Git then waits for the command there.

An empty `.git` that only reserves the name is not a repository and stays
held by the helper like every other reserved entry. So does a repository
the engine's fixed workers handle: the Git job decides for itself what it
may write.

That leaves a project without a repository as it was. The person's
`git init` there fails while a command runs (`could not write config file
...: Permission denied`) and works once the command has ended or the
background command is stopped.

### What the model is told

The model learns the shell and its syntax from one line of the session
context. For this sandbox the line now also states what a command meets
there, so that the model does not have to find it out one failed command at
a time. In the live runs it had reported the reserved entries as "three
empty instruction files" of the person's project, called installed tools
"not available", and looked for a POSIX shell after `&&` failed. The added
text, verbatim:

> This is Windows PowerShell 5.1: && and || do not exist, so chain with ; or
> if ($?) { ... }. In a PowerShell error, line 2 is the first line of your
> command. Git Bash, sh and WSL do not start in this sandbox. Programs on
> the user's PATH are available, with what they keep in the user's home
> folder, in APPDATA and in Program Files: those are the user's real
> folders, readable and never writable. Credential folders, the user's
> documents, browser and chat-app profiles and the user's own local
> application data are not readable. A program that is installed but not
> found, or that cannot read its own files, is in a folder the sandbox does
> not grant: request permissions.read_folders for that folder. LOCALAPPDATA
> and TEMP are private scratch that is deleted when the command ends. A tool
> that has to write in the home folder or in APPDATA is refused with an
> access error: a global install, or a package cache there
> (~/.cargo/registry, ~/go/pkg/mod) when a dependency is not downloaded yet.
> Install into the project (a virtual environment, node_modules), or request
> permissions.write_folders for that one folder. cargo cannot resolve host
> names with permissions.network alone: request permissions.local_preview
> with it. Not reachable from the sandbox: the clipboard, other programs
> through COM (Word, Excel), Windows services, WMI/CIM, and opening a page in
> the browser. Say so instead of retrying. Get-PSDrive shows no free space
> there; [System.IO.DriveInfo] does. Without permissions.local_preview every
> connection to 127.0.0.1
> is refused with an access error, also to a server the same command
> started: request it for tests and tools that use a local port. The sandbox
> reserves these paths in the project folder: AGENTS.md, CLAUDE.md,
> CONVENTIONS.md, .git, .alysis, .sylliptor, .codex, .claude, .agents,
> .alysis_skills, .github/skills. Empty entries for them, and an empty
> folder above a nested one, appear while a command runs, and for as long as
> a background command runs. An empty one is the sandbox's, not the user's:
> do not report it as project content, and do not edit or delete it.

Each sentence states something measured on the real sandbox. The legacy
`windows-native` line is unchanged.

### Errors that quote the command

The engine prepares the shell before the command runs: it maps the project to
a drive of its own, enters the working directory and restores the temp
folder. That preparation and the command used to be one line. PowerShell
quotes the line an error is on, so a mistake in a command was reported as

```text
At line:1 char:378
+ ... Location -LiteralPath 'Alysis:\' -ErrorAction Stop; ls -la && cat REA ...
```

with the engine's own text in front and a column nobody who wrote the command
could use. The command now starts on a line of its own, and the same mistake
reads

```text
At line:2 char:8
+ ls -la && cat README.md
```

The column is the command's own. The line number counts the preparation as
line 1, so a command's first line is reported as line 2.

## Commands in the sandbox follow the mode

The sandbox is the boundary for a command, as it is in Codex. A command that
runs inside the MXC sandbox follows the mode: in "Approve for me" and with
`--yes` it runs without a card, and in "Ask for approval" it gets the ordinary
card, which can be allowed for the session. What a command needs beyond the
sandbox it asks for (`permissions` on `shell_run` and `shell_background`):
network access, a local preview, more folders. That request goes through the
access review: the reviewer in "Approve for me", a person in "Ask for
approval". The access review is the decision about that access, so no second
mandatory card follows it.

Access can also be approved for the rest of the turn or the session, or saved
as a profile. That review was about the command that asked. A later command
that inherits network access or access to servers on this computer is
therefore decided again, with its own text: by the reviewer in "Approve for
me", by a person in "Ask for approval", and a command that only looks is not
exempt, because with a route out reading is sending. Folders approved for the
session open no route out and are not decided again.

Before this change every Windows command needed its own person decision,
which the reviewer could not give and which could not be allowed for the
session. PowerShell's aliases, functions and profiles cannot be resolved from
the text of a command, so the rule that reads commands could not rule out a
remote write and asked every time. In a live run of the desktop app in
"Approve for me", 40 of 40 commands stopped for a person. Eight of those
tasks run again with this change gave 3 of 26: the three literal Git writes
of one task. Nothing reads the text for this any more: what decides is what
executes the command.

A command follows the mode when all of this is true:

- the launcher's settings select `windows-mxc` in strict mode, and the runner
  that executes the command is that runner (an unresolved lazy runner is
  judged by the settings its loader captured, without starting a helper);
- the helper is the security revision that was probed (revision 11,
  `mxc-psec`);
- the launcher's own settings have networking off and no loopback preview;
- nothing gives the command credentials or repository metadata: no exact
  read files, forwarded environment, SSH agent, GitHub authentication,
  sockets, private endpoints or writable Git metadata, and the environment is
  cleared.

Approved folders, read or write, do not change this: they are part of the
boundary the person or the reviewer already accepted.

Following the mode means the mode's own checks now apply to a Windows
command, where the mandatory card used to come first: a command that only
looks runs without a card in "Ask for approval", a sensitive command
(publishing, changing shared systems) goes to the reviewer in "Approve for
me", and deleting the project or home folder is always asked about. These
checks read the command in the dialect of the shell that runs it. In cmd.exe
`echo ok; npm publish` is one harmless command; in PowerShell it is two.

### A forced recursive delete is decided, not refused

A forced recursive delete (`Remove-Item -Recurse -Force`, `rd /s /q`,
`del /s /q`) is refused outright by the command policy, in every mode but
Full access: without a boundary one wrong path removes the person's files.
In a live run that refusal came after the person had approved the command,
and the agent left 1,862 files of a throwaway environment in the project
because it had no way to remove a folder.

In this sandbox the command can delete only inside the approved folders and
its own scratch folder. So the question is the mode's, as a forced delete is
in Codex: the reviewer decides it in "Approve for me", the person in "Ask for
approval". It is decided every time: `--yes` does not waive it, it cannot be
allowed for the session, and access that was reviewed for the same command
(network, a folder) does not count as a decision about the delete. Without
anyone to ask it stays refused. Whoever decides sees the command and that it
is a recursive delete.

Unchanged:

- a delete of a drive, a system folder or a profile, drive formatting and an
  unreadable encoded command stay refused;
- a delete aimed at the project folder, a folder above it or the home folder
  is asked about as before, now also when it is forced;
- outside this sandbox (the host fallback, the legacy helper, networking the
  launcher turned on, an unprobed helper revision) the refusal stands;
- `rm` with recursive and force options has its own rule, on every system:
  it is decided every time in the same way (reviewer or person, never
  `--yes`, never for the session) wherever the command runs, and refused for
  a root, a home folder or a folder directly under the root. Written the
  PowerShell way (`rm -r -fo`) it is one of the Windows spellings above.

### Staging, committing and branching follow the mode

A literal Git command that changes the repository (`git add`, `git commit`,
`git switch -c`, `git merge`, `git stash`, `git tag`) used to stop for a
person every time, in every mode, and could not be allowed for the session.
The shell tool sends such a command to a separate Git job, and that job got
the whole Git directory to write. A Git directory holds what decides which
programs a later Git runs: its configuration (`core.fsmonitor`, an alias, a
filter, `core.hooksPath`), its hooks, the command list of a rebase. A hook
that a commit starts could have left a program there for the person's own
Git, which runs outside the sandbox. In the live run above these were the
three commands of 26 that still stopped.

The Git job now withholds exactly that part, as the macOS sandbox does since
its own change. The command writes the repository's objects, refs and index.
It cannot change, create or replace:

| In the Git directory | Why |
| --- | --- |
| `config`, `config.worktree` | Settings that name programs Git runs |
| `hooks` | Programs Git runs |
| `commondir` | Says where configuration and hooks are read from |
| `rebase-merge` | The command list of a rebase, which may hold `exec` lines |
| `remotes`, `branches` | The old way to define a remote, and a remote can name a program |
| `modules`, `worktrees` | The Git directories of submodules and of other worktrees, each with the same names in it |

The same holds for everything the command starts: a hook, an alias, a
filter, a signing program. So a command in the Git job follows the mode like
any other command in the sandbox. Measured with a commit whose hook tries
twenty ways: append to `config`, also spelled `CONFIG` and through a hard
link; write and replace a hook; rename `config`, `hooks` and `.git` itself
away; rename a prepared file onto `config` and onto `commondir`, and a
prepared folder with a command list onto `rebase-merge`;
`git config alias...`; create `commondir`, `config.worktree` and
`rebase-merge`; define a remote under `remotes` and `branches`; make a
folder under `modules` and `worktrees`. All twenty are refused, a control
write beside them succeeds, the commit is made, and the configuration and
hooks are byte-identical afterwards.

| | Before | Now |
| --- | --- | --- |
| `git add`, `commit`, `switch -c`, `merge`, `cherry-pick`, `revert`, `stash`, `tag`, `rm`, `mv`, `fetch`, a plain `git init` in "Approve for me" or with `--yes` | A person, every time | No card |
| The same in "Ask for approval" | A person, every time | The ordinary card, which can be allowed for the session |
| `git reset --hard`, `branch -D`, `clean -f`, `checkout -- file`, `restore`, `stash drop` in "Approve for me" | A person | The reviewer, as for every command that throws work away |
| `git push` | A person | A person: what it changes is not in the sandbox |
| `git clone`, `git init` with a template, a separate Git directory or `--bare` | A person | A person: the repository they write does not exist yet, so nothing in it can be kept as it is |
| A plain `git init` in a folder that already holds a Git directory with anything in it | A person | No card, and run like any other change to that repository: its configuration and hooks stay as they are, so Git reports that it could not write them |
| `git config ...`, `git remote add ...` | Not possible: refused in the general shell, and the Git job did not take them | With `permissions.git_metadata=true`, which a person approves |
| `git rebase`, recording an upstream, renaming a branch, `git worktree add`, `git submodule add` | A person | With `permissions.git_metadata=true`, which a person approves |

Run live in the desktop app in "Approve for me" with a real model, four
tasks: a commit on a new branch, and a commit, merge, tag and branch
deletion in a row, both finished without a person card (the earlier run
stopped three times for the first). A rebase asked a person once, and
setting the repository's user name and adding a remote asked twice: the
model requested `permissions.git_metadata` for exactly those commands. It
read Git's note about the configuration after deleting a branch as what it
is, and went on.

#### How each name is kept

This sandbox refuses a path only when the path exists, so the three cases
are handled differently.

- **A name that exists** is write-protected like every protected path.
- **A name that does not exist and that Git does not mind seeing empty**
  (`config.worktree`, `remotes`, `branches`, `modules`, `worktrees`, and
  `hooks` or `config` where someone removed them) gets an empty entry for
  the command, write-protected and removed again afterwards, the way the
  reserved names in the project are.
- **`commondir` and `rebase-merge`** cannot stand empty: Git stops at an
  empty `commondir` ("failed to read .git/commondir") and takes an empty
  `rebase-merge` folder for a rebase in progress ("cannot switch branch
  while rebasing"). The engine takes these two names instead: it creates
  the entry and marks it for deletion while keeping it open. Windows then
  refuses to open the name or to create anything under it, for every
  program, and removes it when the engine lets go, also when the engine
  dies. Git is told the name cannot be opened and treats it as absent.
  Nothing can be renamed onto such a name either, with the classic rename
  or the one that replaces an open file.
- **The Git directories of other worktrees** are protected through the
  folder that holds them, `worktrees`. From the main worktree that folder
  is write-protected like any name that exists. From a linked worktree the
  command's own Git directory lies in it, and a deny would refuse that one
  too, so the folder gets a read-only rule and only the command's own Git
  directory is writable inside. Either way no handle is on another
  worktree's own Git directory, so its Git replaces its index and `HEAD`
  there while this command runs, and a worktree somebody adds meanwhile is
  covered. Measured from both sides, with a commit that is still running
  its hook in one worktree while the person's Git commits in another and
  adds a third: the hook sees the new one and can change nothing in it.

A plain `git init` creates a repository whose configuration and hooks Git
writes itself, so it follows the mode. That is true only where no
repository is: Git 2.56 runs a `reference-transaction` hook that is already
in the folder while it creates `HEAD`. So the job looks at the folder. Where
the Git directory exists with anything in it, the command is treated as a
change to that repository. And nothing of a repository that exists is
given to a command that creates another one, also when the new one lies
inside it: `git init docs` in a project leaves the project's own Git
directory write-protected as a whole. A `.git` entry that is a link is
never taken for a new repository, and one that leads nowhere is refused.

One Git command at a time changes a repository, whichever of its worktrees
it runs in: what is kept in place in the shared Git directory is kept for
one command. A second one waits for the first, as two in the same project
always did. A name that is taken does not stop an inspection or the next
command from finding the repository.

#### Asking for the rest

`permissions.git_metadata=true` on the `shell_run` call gives one literal
Git command the whole Git directory. A person approves it, in every mode;
it cannot be approved for the turn or the session, or saved. With it the Git
job also takes `git config` and `git remote`, which change nothing but
configuration. It is refused for a command it would change nothing for: an
inspection, a script, Git reached through another program. The
configuration of the person's account and of the machine (`--global`,
`--system`) is not changed from here in any case.

Where a command cannot ask for permissions at all (Full access with the
sandbox on, a subagent, a one-shot run) and a person can be asked, nothing
changes: a Git change keeps the person there and gets the whole Git
directory, or a rebase could not be run at all. The same goes for every Git
command a person decides on for another reason (a push, launcher
networking, GitHub authentication): the person's yes gives it the whole Git
directory, as before. Where nobody can be asked, a Git change was refused
before and now runs like the rest, without the names above.

Git reports what it was refused itself, and not always with a failure, so
every result of the Git job says which policy applied and what lifts it.
Measured in the sandbox, without the permission:

| Command | What happens |
| --- | --- |
| `git rebase main` | Nothing is changed. Git prints `error: could not create temporary .git/rebase-merge: Permission denied` and exits 0 |
| `git switch -c topic origin/main`, `git checkout -b topic origin/main` | The branch is made and checked out; that it follows `origin/main` is not recorded (`error: could not write config file .git/config: Permission denied`, exit 0). `--no-track` makes the same branch without the error |
| `git branch --set-upstream-to=...`, `git push -u` | The same error, exit 0; nothing recorded |
| `git branch -D topic` | The branch is deleted; Git adds that it could not update its configuration (exit 0) |
| `git branch -m old new` | The branch is renamed; its settings keep the old name (`fatal: branch is renamed, but update of config-file failed`, exit 128) |
| `git worktree add`, `git submodule add` | Refused when Git creates the new Git directory (exit 128). `worktree add -b` has made the branch by then |
| `git rebase --apply main` | Works: this kind of rebase keeps no command list. `git pull --rebase` is refused like `git rebase` once there is something to replay |
| `git config user.name ...` in the general shell | Refused there as before, now with the note that the permission exists |

#### What a person may notice, and what is not covered

- While a Git command of the agent runs, the person's own Git in the same
  repository cannot change its configuration or hooks, or start a rebase
  ("could not write config file", "could not create temporary
  .git/rebase-merge"). It works again when the command has ended, usually
  within a second; a slow hook makes it longer. Reading, staging and
  committing are not affected, in this worktree or another.
- A program that opens `.git/commondir` itself, and does not treat "cannot
  be opened" as "not there", fails during that time. The engine's own Git
  reader did, and reads past the name now. Git for Windows does not: with
  its folder-listing cache on (`core.fscache`, the installer's default)
  `status`, `add`, `commit`, `switch`, `diff` and `worktree list` were run
  while both names were taken, and none noticed.
- The files of the repository's history (objects, refs, the index) are
  writable for a hook that a Git command starts, as they are for Git itself.
  Before, a person had decided on every such command.
- A file inside the Git directory that the configuration itself names
  (`include.path = ../config.local`, a `core.hooksPath` that points into
  `.git`) is not among the protected names.
- The `.git` file of a linked worktree or submodule that lies in a
  subfolder of the project is a project file, as it is for every other
  command.
- A repository in a subfolder of the project is not protected by any of
  this, as before: see the limits below.
- Hooks that live in the work tree (`core.hooksPath` pointing at a tracked
  folder, as Husky sets it up) are project files like any other.

### What still needs a person

- **A literal `git push`, `git clone`, and a `git init` that is more than a
  plain one.** A push changes a remote; the other two write a repository
  that does not exist yet. See "Staging, committing and branching follow
  the mode" above for what no longer needs a person. The command guard and
  the shell tool ask the same function (`git_native.shell_git_route`) which
  route a command takes. What the Git job gives it the guard asks
  `programs_exposed`; the job reads the same words and gives no more than
  that says. Git reached any other way (`cmd /c git ...`, the
  call operator, two statements, Python) runs in the general shell like
  every other command, where the repository metadata is write-protected
  and the write fails.
- **A Git command that asks for the whole Git directory**
  (`permissions.git_metadata`): configuration, remotes, a rebase, worktree
  and submodule setup.
- **Every grant that carries credentials.** On this backend those are GitHub
  authentication (for a literal Git command) and exact read files, which may
  hold a secret. A person decides the access and then the command; the
  reviewer does not stand in. Forwarded environment variables, daemon
  sockets and domain lists cannot be requested for this sandbox at all, and
  a scope that carried one would keep the person.
- **Networking or a local preview that the launcher turned on itself.** Nobody
  reviewed that access for this command. Access requested for one command and
  reviewed is different, and follows the paragraph above.
- **Any command while a `.git` file names a Git directory the sandbox cannot
  write-protect**: one that is an approved folder, that contains an approved
  folder (its `hooks`, say), or that is the project folder itself.
- **Everything that is not this sandbox**: the host fallback, the legacy
  `windows-native` helper, another backend, a subclass of the runner, a
  durable service (started by another manager with its own settings), and a
  helper whose security revision was not probed.

A literal Git command that only inspects (`git status`, `git diff`,
`git log`) needs no person. What makes that safe is the job it runs in, not
the spelling: nothing is writable there and it has no network, so a command
spelled to write (`git log --output=file`) or to start a program fails or
runs contained.

Full access with the sandbox off is not covered by this: it has no sandbox,
so its commands still take the path described in `shell_sandbox.md`.

### What the sandbox refuses and what it leaves reachable

Probed on 2026-11-11 on Windows 11 (build 26300) against the signed helper
(protocol 4, security revision 11), with the settings the app uses for
"Approve for me": strict, workspace-write, network off, default preview
access, no grants. The helper packaged with the engine and the one the
desktop app ships gave the same results. Host listeners, pipes, files and
registry values were real, and each receiver was first reached by the host
itself, so silence means refusal. The command ran as the signed-in user in an
app container at low integrity.

| Attempt from inside the sandbox | Result |
| --- | --- |
| TCP to host loopback (IPv4, IPv6), the LAN address, a public address | Refused (`WSAEACCES`); nothing arrived |
| UDP to host loopback, the LAN address, a public address | Send call returns; nothing arrived, and a 3.6 MB burst added no traffic on the adapter (the same burst from the host added 3.75 MB) |
| Raw ICMP to loopback, the gateway, a public address | No reply |
| Resolve a public name (API, `Resolve-DnsName`, `nslookup`, `ssh`, `curl`, `git ls-remote`) | Failed every time |
| Listen on loopback while the host connects in | Host connection times out |
| Connect to its own listener on loopback | Refused |
| `\\127.0.0.1\C$`, a WebDAV path on a host listener, another machine's share | Refused; the listener saw nothing |
| Host pipe with default security, open to Everyone, or open to Everyone at low integrity | Refused |
| **Host pipe that admits every app container** | **Opened and written** |
| WMI queries and process creation, Task Scheduler, listing services (`Get-Service`), BITS, `ShellWindows`, Office and Update COM servers | Refused (`sc query` of one named service still answers) |
| Open a URL through `Start-Process`, `cmd /c start`, `rundll32`, `explorer.exe`; `ShellExecute`, `WScript.Shell.Run` writing outside | No request reached the host listener; nothing was written outside |
| Open the engine process (write memory, duplicate handles, terminate) | Refused |
| Environment of the engine process, including a secret set there | Not passed on; a fixed list of names only |
| Credential Manager, Kerberos tickets, `cmdkey` | Refused or empty |
| `~/.ssh`, a credential file inside a tool's folder (also by its 8.3 name and with a `\\?\` path), a browser profile inside a tool's folder, the person's documents, a messenger's and a chat app's folder in the roaming data, browser and DPAPI key stores, a file beside the project | Refused |
| `.gitconfig`, a tool's folder in the home, the roaming data and the ProgramData folder of a program on the PATH, Program Files | Read since the change described in "The person's home and installed programs"; never written |
| A folder on the person's PATH, and the installation of one under Program Files | Read since this change; never written |
| Clipboard text | Refused |
| **A variable saved with `setx` (`HKCU\Environment`)** | **Read** |
| **Decrypt a blob the user protected with DPAPI** | **Decrypted** |
| Write under `HKCU` (a new key, the Run key, `HKCU\Environment`) | Refused |
| Write to the Startup folder, the profile, `AppData`, the host temp folder, beside the project | Refused (the command's own scratch folder is the one place outside the approved folders it can write) |
| Change the project's own `.git` by a direct write, another spelling (`.GIT`, trailing dot, 8.3 name, `\\?\`, `\\.\`), an alternate data stream, a junction, a symbolic link, a hard link, a substituted drive, a rename or delete of the folder or its parent, `icacls`, `takeown`, or Git reached through `cmd`, the call operator or Python | Refused; the host found the folder byte-identical, also now that the helper no longer holds the repository open (see "Git keeps working while a command runs") |
| The same against a repository the project's `.git` file points to inside the project: `--separate-git-dir`, also under a folder named `.git`, and the shared repository of a linked worktree | Refused since this change; writable before it |
| The same against a worktree's repository outside the project, or under the project's own `.git` | Refused |
| **Create or change a repository in a subfolder (`sub\.git\hooks`, `sub\.git\config`)** | **Written** |
| `git log --output=file` in the read-only Git job | Nothing written |

Four things are still reachable. They are known limits of this sandbox, not
reasons for a card: a card on every command did not make them safer.

- **Host pipes that admit every app container.** Windows lets a host process
  open its pipe to all app containers. Crash handlers of browser-based apps do
  this for their own sandboxed children: on the probed machine eleven such
  pipes and the RPC endpoint mapper could be opened. A crash handler uploads
  what a client hands it to its vendor's server: a fixed destination, without
  the user's credentials. The probes opened these pipes and sent nothing.
- **Saved environment variables.** The process environment is cleared, but
  variables saved with `setx` are readable from `HKCU\Environment`. API keys
  are often kept there. With networking off the command has nowhere to send
  one; a command that asks for network access is reviewed with its full text.
- **The user's DPAPI key.** The command can decrypt any blob protected for the
  user. The usual blob stores are unreadable, so this matters for blobs inside
  the project or the readable part of the registry.
- **Repository metadata below the project root.** The sandbox write-protects
  exact paths, so the project's own `.git`, and the repository its `.git` file
  points to, are covered, and a repository in a subfolder is not: a command
  can write hooks and configuration into an existing one, or create one. Git
  run later on the host in that subfolder, or through a submodule, would act
  on them. macOS denies the name `.git` at any depth; this helper cannot deny
  a path that does not exist yet.

One more limit is not specific to this sandbox: a repository whose
configuration points Git at files in the working tree (`core.hooksPath`, an
`include.path`) lets any command that can write the project change what Git
runs later on the host. Protecting `.git` does not cover that, here or in
Codex.

`windows_mxc_containment.py` lists these four, and one probe test fails when
that list and the sandbox disagree in either direction. Closing the first
three needs support in the upstream runtime: an unsigned local build of the
same source, used only for this measurement, showed that the less-privileged
app container is refused on the path the helper uses, and that dropping the
`registryRead` capability changes nothing. The fourth needs the helper to
deny a name wherever it appears.

### How this compares with Codex

Read from the Codex source (not run), November 2026. With its default
approval policy and a restricted sandbox, Codex runs a command in the sandbox
without asking, and asks only when the model requests to leave the sandbox or
the command matches a dangerous pattern; a reviewer model can answer those
requests instead of the person. That is the behaviour this change brings to
Windows.

The boundary itself is at least as tight here. Codex's Windows sandbox leaves
the whole disk readable and its unelevated mode blocks networking through
environment variables only; neither mode handles Credential Manager, DPAPI,
`HKCU` or pipes. It protects the top-level `.git` that exists at launch and
follows a `gitdir:` pointer, and has no rule for a repository in a subfolder,
which is the fourth limit above.

Two differences remain on purpose and are the owner's to decide: Codex has no
rule that a push needs a person (its reviewer may approve one), and it
treats networking the launcher turned on like any other sandbox setting.
For other Git writes the two now differ the other way round: Codex keeps
the whole `.git` read-only, so a commit there is a request to leave the
sandbox, and here a commit runs inside it.

Codex passes the person's PATH on unchanged, and with the whole disk readable
every program on it runs. Here the PATH's own folders, the tool folders of
the home and of the roaming data, and Program Files are readable and the
rest of the disk is not. A program that keeps its files anywhere else (a
toolchain on another drive, a version manager in the local application
data) does not run until that folder is requested.

Codex gives a command the person's real home to read and the person's real
temp folder to write. Here the home is the person's as well, without the
credential folders Codex's elevated Windows sandbox leaves out too, and
without more that Codex leaves readable: the person's documents, browser
and chat-app profiles, other agents' homes, and the application data of
programs that are not on the PATH. The temp folder and the local application
data are the command's own, so nothing a command leaves there is found by
another program. Codex sets nothing up for a single language either. Where
a toolchain has to write in the home, its model asks to leave the sandbox;
here the model asks for that one folder.

One consequence to know: a folder request is decided by the access review
alone, as an escalation is in Codex. In "Approve for me" that is the
reviewer, also for a folder that holds a secret or one that Windows runs
programs from at sign-in. Exact read files keep the person; folders do not,
unless the path is on the engine's list of sensitive locations.

### Verdict per backend

- `windows-mxc`: a sandboxed command follows the mode, with the exceptions
  above.
- `windows-native` (the legacy helper): not probed. Its helper is not packaged
  with the engine, so no command was run in it. Every command keeps the
  person decision.

### Repeating the probes

```powershell
$env:ALYSIS_RUN_CONTAINED_PROBES = '1'
python -m pytest -p no:cacheprovider tests/test_windows_contained_probes.py
```

The probes use synthetic data only. They create and remove one value under
`HKCU\Environment` and listen on loopback ports while they run. The UDP
burst, the clipboard and the sweep of the machine's own pipes were run by
hand and are not repeated by the tests. A helper with a new security revision
keeps the person decision until it has been probed and
`PROBED_SECURITY_REVISION` is moved.

## Release maintenance

### Experimental `windows-native` backend

The separately selected `windows-native` preview requires elevated native setup.
Its revision-6 helper uses a less privileged AppContainer and preserves the
independent write restrictions. PowerShell receives registry-read, event-
instrumentation and COM capabilities; .NET Framework workers require COM
initialization. The write restriction includes the ordinary user identity needed
for Windows capability checks while retaining account-based filesystem denies.
LPAC grants still constrain which resources that user can access. Commands verify effective
capability access under the suspended child token before running. Network
capability still follows explicit off/on policy. Network-on also grants
`lpacIdentityServices`, needed to discover Schannel's TLS package through the
Windows security service. Network-off omits it and verifies effective denial.
Enterprise authentication, shared user certificates, private-network and server
capabilities remain absent and are checked under the actual child token.

This preview still has compatibility limits. Schannel HTTPS (including Windows
PowerShell web requests and Windows curl) fails with `SEC_E_NO_CREDENTIALS`
under the independent write-restricted token. Node HTTPS uses a separate TLS
implementation and passes the explicit network-on/off/on checks. Node builds
with libuv older than 1.53 can hang when launching subprocesses because they use
an ordinary pipe namespace that AppContainer denies. These are existing expected
failures in preview CI, not evidence of full compatibility. The default
`windows-mxc` backend is separate; passing preview jobs does not promote the
experimental backend to release-qualified status.

Setup version 4 also grants an installation-specific capability read/write access
to the fixed Windows null device, which Git and subprocess redirection require.
The grant has no effect on other devices or files. Commands verify access using
the actual suspended child token; ordinary execution never changes device ACLs.
Uninstall removes only the installation's grant. Windows can reset device ACLs
after a reboot. If readiness reports missing null-device access, rerun
`alysis sandbox setup --native-windows` with elevation before using this preview.
This setup requirement applies to the experimental backend, separately from the
default `windows-mxc` installation described above.

### Local source qualification

When current native source has advanced beyond the signed payload, build a fresh
candidate with `uv run python -m alysis_code.windows_mxc_candidate_builder
--output C:\path\to\new-bundle` into a new absolute directory. The archived
`scripts/build_windows_mxc.py` recipe is retained for the signed bundle's source
inventory; use the module above for new candidates. The inventory
includes the wheel hook as well as the native helper, engine and builder inputs.
For a local standard wheel, set `ALYSIS_LOCAL_CANDIDATE=1`,
`ALYSIS_WINDOWS_SANDBOX_BUNDLE` to that absolute directory and
`ALYSIS_WINDOWS_SANDBOX_BUNDLE_SHA256` to the independent digest returned by the
build. The checkout must be clean and committed before building the candidate.

The wheel hook verifies the exact policy, binary, complete source inventory and
commit, then copies the verified bytes into the wheel. Its private
`_windows_mxc_local.json` records the candidate pins; the installed CLI uses this
bundled helper without Desktop environment overrides. Checked-in release assets
and their pins are unchanged. Source changes during packaging, changed helper
bytes, aliases, hardlinks and missing provenance fail the build.

Local wheels remain `prototype-unqualified`, do not claim a signing identity,
and are rejected by release validation. Source distributions, editable builds
and release packaging cannot use this route. A passing local qualification does
not promote a signed release or change the supported Windows matrix.

`src/alysis_code/assets/windows-mxc` contains the immutable signed helper,
manifest, source inventory and Microsoft's license. The universal Python wheel
and source distribution include these assets; other operating systems do not
execute them. Frozen packaging uses `--collect-all alysis_code`.
`windows_mxc_bundle.py` pins the exact bytes independently of the sidecar files;
`hatch_build.py` rejects a missing or altered payload before packaging.

The bundle's `manifest.json` records its protocol, security revision and source
commit. `SOURCE.json` records the build inputs, and `windows_mxc_bundle.py` pins
the executable and metadata hashes. Consult those files for the exact helper
included in this checkout.

Python-only changes can reuse the helper while its native inputs remain unchanged.
A native-code or policy-contract change requires a rebuilt, tested and signed
helper, followed by reviewed updates to the pins. Startup must not automatically
download or trust an unsigned replacement.

An explicit helper override still requires both an absolute helper path and its
SHA-256, for existing Desktop launchers and development qualification. Partial
overrides fail closed. If a package manager hardlinks the helper itself, only
the pinned executable is copied atomically to a private digest-specific cache;
the shared package directory is never granted as a replacement helper root.

Before release, test the desktop installer, upgrades and antivirus behavior
separately. Local CLI checks cover the tested machine and configuration.
