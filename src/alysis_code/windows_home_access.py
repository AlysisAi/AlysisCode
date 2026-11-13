"""What a command in the Windows sandbox can read of the person's home and installed programs.

A language toolchain is more than the program on the PATH. rustup's ``cargo``
finds its compiler in ``~/.rustup`` and the crates it has downloaded in
``~/.cargo``; uv finds its Pythons in the roaming application data; a compiler
finds the linker and the SDK in Program Files. With only the PATH folders
readable and the home folder replaced by an empty scratch folder, every tool
of that kind was found and then failed.

No tool is named here. A command is told where the person's home and roaming
application data really are, and can read what programs keep there, never
write it. That is the arrangement Codex uses, and the one Windows itself
makes for a sandboxed app: the profile stays where it is, and the app gets a
local application data and a temp folder of its own.

Left unreadable, because no toolchain needs them and they are the person's:

- credential folders and credential files, by name;
- the homes of other coding agents, which hold their sign-ins and what was
  said to them;
- the folder of a browser, or of an app built on a browser engine (chat and
  mail clients, editors): each holds the app's cookies and saved sign-ins,
  and a command in this sandbox can decrypt what Windows protected for the
  user (``docs/windows-cli-sandbox.md``);
- the person's document folders, and any other folder in the profile that
  is not a tool's: one whose name does not start with a dot is readable
  only when a program on the PATH lives in it (``~/go``, ``~/scoop``);
- in the roaming application data and in ProgramData, every folder that is
  not a tool's. Messengers, remote-desktop tools and games keep their
  sessions there, and nothing in the folder says so. A folder counts as a
  tool's when a program on the PATH lives in it, when it is named like a
  program on the PATH (``uv``, ``npm``, ``Python``), or like one of the tool
  folders in the home (``.nuget`` and ``NuGet``);
- the local application data, apart from the PATH folders in it;
- a folder that holds a project, the scratch folder, or anything of the
  application's own. What lies beside those in the same folder stays
  unreadable too.

A PATH folder the command can write is no evidence: a command could put a
file there with the name of the folder it wants to read.
"""

from __future__ import annotations

import functools
import os
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .sensitive_paths import sensitive_path_category
from .windows_path_tools import _plain

# The first twelve are the folders Codex leaves out of the profile it makes
# readable (windows-sandbox-rs, USERPROFILE_ROOT_EXCLUSIONS). The rest are
# this engine's own state and the places Windows and common command line
# tools keep credentials, in the profile or in the roaming application data.
_CREDENTIAL_FOLDERS = frozenset(
    {
        ".ssh",
        ".tsh",
        ".brev",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".config",
        ".npm",
        ".pki",
        ".terraform.d",
        ".alysis",
        ".sylliptor",
        ".mcp-auth",
        "microsoft",
        "gnupg",
        "gh",
        "github cli",
        "gcloud",
    }
)
# In ProgramData: the configuration and host keys of the SSH server.
_PROGRAM_PRIVATE_FOLDERS = frozenset({"ssh"})
# Credential files the engine's own list (``sensitive_paths``) does not name.
# A build tool's settings file that may also hold a credential is not here
# (Maven's settings.xml, gradle.properties, NuGet.Config, pip.ini): the tool
# stops without it.
_CREDENTIAL_FILES = frozenset(
    {
        "credentials.toml",
        ".credentials.json",
        "oauth_creds.json",
        "auth.toml",
        "hosts.yml",
        "kaggle.json",
        "token",
        "settings-security.xml",
        "rclone.conf",
        ".mylogin.cnf",
        ".pgpass",
        "pgpass.conf",
        ".my.cnf",
        ".boto",
        ".s3cfg",
        ".dockercfg",
        ".terraformrc",
        ".databrickscfg",
        ".renviron",
        ".hgrc",
        ".yarnrc",
        ".yarnrc.yml",
        ".sentryclirc",
        ".wakatime.cfg",
        ".b2_account_info",
        ".erlang.cookie",
    }
)
# A name that says what the entry holds. "history" is what shells and
# interpreters call the file of everything that was typed into them.
_CREDENTIAL_WORDS = ("credential", "secret", "password", "passwd", "token", "history")
# Where other coding agents keep their sign-ins and conversations: the name,
# and what starts with it (".claude", ".claude.json", ".aider.chat.history.md").
# In the roaming application data the same names without the dot.
_OTHER_AGENTS = (
    ".claude",
    ".codex",
    ".gemini",
    ".copilot",
    ".cursor",
    ".continue",
    ".codeium",
    ".windsurf",
    ".aider",
    ".openai",
    ".anthropic",
    ".opencode",
)
# What a browser engine keeps at the top of a profile.
_BROWSER_PROFILE_FILES = frozenset(
    {"local state", "cookies", "login data", "profiles.ini", "logins.json", "key4.db"}
)
# The folders Windows makes in every profile for the person's own files, and
# the folders of file-syncing services. Windows' names are the same in every
# display language.
_PERSONAL_FOLDERS = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "pictures",
        "music",
        "videos",
        "favorites",
        "contacts",
        "links",
        "searches",
        "saved games",
        "3d objects",
        "appdata",
        "box",
        "mega",
    }
)
_SYNCED_FOLDERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "nextcloud")
# What the folder of an installation that holds its programs is called. A
# PATH folder with another name says nothing about the folder it lies in:
# "~/projects/scripts" does not make "~/projects" a tool's.
_PROGRAM_FOLDER_NAMES = frozenset({"bin", "shims", "cmd", "condabin"})
_PROGRAM_EXTENSIONS = (".exe", ".cmd", ".bat", ".com", ".ps1")
# A file some program is writing right now. One that is gone when the command
# starts would fail the command.
_TRANSIENT_SUFFIXES = (".lock", ".tmp", ".swp", "~")
_SCAN_LIMIT = 4096
_FILE_LIMIT = 64
# How far below a readable folder a browser profile or a credential file is
# looked for, and how many folders are opened for it. A tool's folder can
# hold a browser's profile several folders down ("~/scoop/persist/firefox").
_DEEP_LEVELS = 3
_DEEP_FOLDERS = 300
_INSPECTION_AGE_S = 900.0


@dataclass(frozen=True)
class HomeAccess:
    """Folders and files to make readable, and what inside them to refuse."""

    read: list[Path] = field(default_factory=list)
    deny: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class _Listing:
    folders: tuple[str, ...]
    files: tuple[str, ...]
    complete: bool


# What a folder held when it was last looked at, by path and modification
# time: a folder's time changes when an entry is added to it or removed.
# Reading a few hundred folders for every command would cost about half a
# second; reading the ones that changed costs a few milliseconds.
_listings: dict[str, tuple[int, _Listing]] = {}
# What must stay unreadable inside a folder, by path, with the folder's
# modification time and the moment it was looked into. Looking several
# folders down is the expensive part and a change there does not show in the
# folder's own time, so the answer is also dropped after a while.
_inspections: dict[str, tuple[int, float, bool, tuple[Path, ...] | None]] = {}
_canonical: dict[str, bool] = {}


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _entries(folder: Path) -> _Listing | None:
    """The plain folders and files directly in ``folder``; links are left out."""
    folders: list[str] = []
    files: list[str] = []
    complete = True
    try:
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries):
                if count >= _SCAN_LIMIT:
                    complete = False
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_link(info):
                    continue
                (folders if stat.S_ISDIR(info.st_mode) else files).append(entry.name)
    except OSError:
        return None
    return _Listing(tuple(sorted(folders)), tuple(sorted(files)), complete)


def _list(folder: Path) -> _Listing | None:
    try:
        changed = os.lstat(folder).st_mtime_ns
    except OSError:
        return None
    key = str(folder)
    cached = _listings.get(key)
    if cached is not None and cached[0] == changed:
        return cached[1]
    listing = _entries(folder)
    if listing is not None:
        _listings[key] = (changed, listing)
    return listing


def _acceptable(path: Path) -> bool:
    """Whether the sandbox helper takes ``path`` in a policy.

    It refuses a path that is spelled differently from what Windows calls it,
    one it cannot open, and one behind a link, and one refused path fails the
    whole command. Whether a path is spelled as Windows spells it does not
    change while the engine runs; that it still exists is checked every time.
    """
    try:
        if _is_link(os.lstat(path)):
            return False
    except OSError:
        return False
    key = str(path)
    known = _canonical.get(key)
    if known is None:
        try:
            # The request is sent as UTF-8; Windows allows names that are not.
            key.encode("utf-8")
            known = (
                _plain(path) and os.path.realpath(path, strict=True).casefold() == key.casefold()
            )
        except (OSError, UnicodeError):
            known = False
        _canonical[key] = known
    return known


def _credential_file(name: str) -> bool:
    """A file that is a credential by its exact name, wherever it lies."""
    lowered = name.casefold()
    return lowered in _CREDENTIAL_FILES or sensitive_path_category(lowered) == "credential_file"


def _credential_name(name: str) -> bool:
    """A folder or file that holds credentials, by what it is called."""
    lowered = name.casefold()
    return (
        lowered in _CREDENTIAL_FOLDERS
        or lowered in _CREDENTIAL_FILES
        or sensitive_path_category(lowered) is not None
        or any(word in lowered for word in _CREDENTIAL_WORDS if word != "token")
        or ("token" in lowered and "tokeniz" not in lowered)
    )


def _other_agent(name: str) -> bool:
    return name.casefold().lstrip(".").startswith(tuple(agent[1:] for agent in _OTHER_AGENTS))


def _local_service(folder: str, parts: tuple[str, ...]) -> bool:
    """A folder with the keys of a program that is running on the computer right now.

    Jupyter writes one file for every running kernel and server, with the
    port and the key that lets a program give it code to run.
    """
    names = (folder.casefold(), *(part.casefold() for part in parts))
    return names[-2:] in {("jupyter", "runtime"), (".jupyter", "runtime")} or (
        names[0] == ".ipython" and len(names) == 3 and names[2] == "security"
    )


def _look_inside(folder: Path, *, words: bool) -> tuple[Path, ...] | None:
    """What inside ``folder`` must stay unreadable, or ``None`` when the folder itself must.

    Directly in the folder a name is enough (``words``): a file called
    "api_token". Further down only what is unmistakable is taken: a browser
    engine's profile, and a file with the exact name of a credential. A
    package cache holds thousands of folders with names like "js-tokens".
    """
    listing = _list(folder)
    if listing is None or not listing.complete:
        return None
    if any(name.casefold() in _BROWSER_PROFILE_FILES for name in listing.files):
        return None
    refused: list[Path] = []
    waiting: list[tuple[Path, tuple[str, ...]]] = []
    for name in listing.files:
        if _credential_name(name) if words else _credential_file(name):
            refused.append(folder / name)
    for name in listing.folders:
        if (_credential_name(name) if words else name.casefold() in _CREDENTIAL_FOLDERS) or (
            _local_service(folder.name, (name,))
        ):
            refused.append(folder / name)
        else:
            waiting.append((folder / name, (name,)))
    looked = 0
    while looked < len(waiting) and looked < _DEEP_FOLDERS:
        current, parts = waiting[looked]
        looked += 1
        below = _entries(current)
        if below is None:
            continue
        if any(name.casefold() in _BROWSER_PROFILE_FILES for name in below.files):
            refused.append(current)
            continue
        refused.extend(current / name for name in below.files if _credential_file(name))
        for name in below.folders:
            if _local_service(folder.name, (*parts, name)):
                refused.append(current / name)
            elif len(parts) < _DEEP_LEVELS:
                waiting.append((current / name, (*parts, name)))
    # What cannot be named to the helper cannot be refused: then the folder
    # that holds it is not readable at all.
    if not all(_acceptable(path) for path in refused):
        return None
    return tuple(refused)


def _inspect(folder: Path, *, words: bool) -> tuple[Path, ...] | None:
    try:
        changed = os.lstat(folder).st_mtime_ns
    except OSError:
        return None
    key = str(folder)
    now = time.monotonic()
    cached = _inspections.get(key)
    if (
        cached is not None
        and cached[0] == changed
        and cached[2] == words
        and now - cached[1] < _INSPECTION_AGE_S
    ):
        return cached[3]
    refused = _look_inside(folder, words=words)
    _inspections[key] = (changed, now, words, refused)
    return refused


def _program_names(programs: Iterable[Path]) -> set[str]:
    """What the programs in the person's PATH folders are called, without the extension."""
    names: set[str] = set()
    for folder in programs:
        listing = _list(folder)
        for name in listing.files if listing is not None else ():
            stem, extension = os.path.splitext(name.casefold())
            if extension in _PROGRAM_EXTENSIONS and stem:
                names.add(stem)
    return names


@functools.lru_cache(maxsize=4096)
def _named_like(name: str, others: frozenset[str]) -> bool:
    """Whether a folder is named like one of ``others``.

    The whole name ("uv"), the name up to a separator ("npm-cache"), or the
    end of a name without separators when that end has five letters or more
    ("pypoetry", "anaconda"). Anything looser matches by accident: "go" in
    "Google", "code" in "opencode", "node" in the "-nodejs" folder every
    Node.js program makes for its own settings, "file" in "FileZilla".
    """
    name = name.casefold().lstrip(".")
    for other in others:
        if name == other:
            return True
        if name.startswith(other) and not name[len(other)].isalnum():
            return True
        if len(other) >= 5 and name.endswith(other) and name.isalnum():
            return True
    return False


def _key(path: Path | str) -> str:
    """A path as text that compares the way Windows compares paths, ending in a separator.

    Comparing a few hundred folders with a dozen others as ``Path`` objects
    took most of the time this module needs for a command.
    """
    return os.path.normcase(str(path)).rstrip("\\/") + "\\"


def _apart(path: Path, apart: Iterable[str]) -> bool:
    """Whether ``path`` contains one of ``apart`` (each a ``_key``) or lies inside one."""
    key = _key(path)
    return any(key.startswith(other) or other.startswith(key) for other in apart)


def _own_profile() -> Path | None:
    """The profile Windows has for the person, whatever the environment says."""
    from .windows_toolchain import known_folder

    return known_folder(0x28)  # CSIDL_PROFILE


def person_home() -> tuple[Path, Path | None] | None:
    """The person's profile and roaming application data, or ``None`` when they cannot be used."""
    from .config import ConfigError
    from .windows_sandbox import _local_path

    profile_value = os.environ.get("USERPROFILE")
    if not profile_value:
        return None
    try:
        profile = _local_path(Path(profile_value))
    except (ConfigError, OSError, RuntimeError, ValueError):
        return None
    if not _acceptable(profile) or profile == Path(profile.anchor):
        return None
    # Inside the sandbox the Windows folder API builds its answers from
    # USERPROFILE. The folder for the command's own local data exists only
    # below the profile Windows itself made for the person. Under any other
    # value a program that asks the API gets an empty answer and writes into
    # the project instead, and the command cannot make the folder: the home
    # is not writable. The scratch home is.
    own = _own_profile()
    if own is None or _key(own) != _key(profile):
        return None
    roaming: Path | None = None
    roaming_value = os.environ.get("APPDATA")
    if roaming_value:
        try:
            roaming = _local_path(Path(roaming_value))
        except (ConfigError, OSError, RuntimeError, ValueError):
            roaming = None
        if roaming is not None and (
            not _acceptable(roaming)
            or roaming == Path(roaming.anchor)
            # A value that names the profile or a folder above it would have
            # the profile's folders judged as application data.
            or _key(profile).startswith(_key(roaming))
        ):
            roaming = None
    return profile, roaming


def home_environment(profile: Path, roaming: Path | None) -> dict[str, str]:
    """The variables that tell a command where the person's home really is."""
    variables = {
        "USERPROFILE": str(profile),
        "HOMEDRIVE": profile.drive,
        "HOMEPATH": str(profile)[len(profile.drive) :],
    }
    if roaming is not None:
        variables["APPDATA"] = str(roaming)
    return variables


def readable_places(
    profile: Path,
    roaming: Path | None,
    *,
    programs: Iterable[Path],
    program_roots: Iterable[Path],
    program_data: Path | None = None,
    apart: Iterable[Path],
) -> HomeAccess:
    """What a command may read outside its project, besides the PATH folders.

    ``programs`` are the folders on the person's PATH that the command cannot
    write. ``program_roots`` are the folders programs are installed in for
    every user (Program Files), ``program_data`` is ProgramData. ``apart`` is
    what a grant may neither contain nor lie inside: the approved folders,
    the scratch folder, what the request denies and the application's and
    engine's own folders. A folder with a problem is left out; nothing here
    fails a command.
    """
    programs = [Path(path) for path in programs]
    program_keys = [_key(path) for path in programs]
    # A PATH folder, and the folder an installation's program folder lies in:
    # "~/go/bin" and "~/go".
    lived_in = {
        *program_keys,
        *(_key(path.parent) for path in programs if path.name.casefold() in _PROGRAM_FOLDER_NAMES),
    }
    apart = [_key(path) for path in apart]
    read: list[Path] = []
    deny: list[Path] = []
    tools: set[str] = set()

    def grant(folder: Path) -> bool:
        key = _key(folder)
        if any(key.startswith(other) for other in apart) or not _acceptable(folder):
            return False
        if any(other.startswith(key) for other in apart):
            return grant_beside(folder)
        # In a PATH folder a name is no sign of a credential: "secretlint.cmd"
        # is a program the command could run before.
        refused = _inspect(folder, words=key not in program_keys)
        if refused is None:
            return False
        read.append(folder)
        deny.extend(path for path in refused if _acceptable(path))
        return True

    def grant_beside(folder: Path) -> bool:
        """``folder`` holds something that stays apart: what lies directly beside that.

        A project in "~/go/src", a package cache the command was let write in
        "~/.cargo/registry", the engine's own environment among uv's tools:
        the rest of the tool's folder is still what the tool needs. Only one
        level down; the folder that holds the thing itself stays unreadable.
        """
        listing = _list(folder)
        if listing is None or not listing.complete or len(listing.files) > _FILE_LIMIT:
            return False
        if any(name.casefold() in _BROWSER_PROFILE_FILES for name in listing.files):
            return False
        granted = False
        for name in listing.folders:
            child = folder / name
            if _credential_name(name) or _apart(child, apart) or not _acceptable(child):
                continue
            refused = _inspect(child, words=True)
            if refused is None:
                continue
            read.append(child)
            deny.extend(path for path in refused if _acceptable(path))
            granted = True
        for name in listing.files:
            if not _credential_name(name) and _acceptable(folder / name):
                read.append(folder / name)
                granted = True
        return granted

    listing = _list(profile)
    if listing is not None:
        for name in listing.folders:
            lowered = name.casefold()
            folder = profile / name
            if (
                _credential_name(name)
                or lowered in _PERSONAL_FOLDERS
                or lowered.startswith((*_SYNCED_FOLDERS, *_OTHER_AGENTS))
            ):
                continue
            # A tool's folder starts with a dot. Any other folder is the
            # person's, unless a program on the PATH lives in it.
            if not name.startswith(".") and _key(folder) not in lived_in:
                continue
            if grant(folder) and name.startswith("."):
                tools.add(lowered.lstrip("."))
        for name in listing.files:
            file = profile / name
            if (
                name.startswith(".")
                and not _credential_name(name)
                and not name.casefold().startswith(_OTHER_AGENTS)
                and not name.casefold().endswith(_TRANSIENT_SUFFIXES)
                and not _apart(file, apart)
                and _acceptable(file)
            ):
                read.append(file)
    known = frozenset(tools | _program_names(programs))

    def a_tools(folder: Path, name: str) -> bool:
        inside = _key(folder)
        return any(program.startswith(inside) for program in program_keys) or _named_like(
            name, known
        )

    if roaming is not None and (listing := _list(roaming)) is not None:
        for name in listing.folders:
            if (
                not _credential_name(name)
                and not _other_agent(name)
                and a_tools(roaming / name, name)
            ):
                grant(roaming / name)
    for root in program_roots:
        root = Path(root)
        if not _apart(root, apart):
            if _acceptable(root):
                read.append(root)
            continue
        # Something that must stay apart lies inside: its neighbours are
        # readable, the folder that holds it is not.
        if any(_key(root).startswith(other) for other in apart):
            continue
        if (listing := _list(root)) is None:
            continue
        for name in listing.folders:
            folder = root / name
            if not _apart(folder, apart) and _acceptable(folder):
                read.append(folder)
    if program_data is not None and (listing := _list(program_data)) is not None:
        # Services keep their settings here, a remote-desktop tool as much as
        # a package manager: a folder needs the same evidence as in the
        # roaming application data.
        for name in listing.folders:
            folder = program_data / name
            if (
                name.casefold() not in _PROGRAM_PRIVATE_FOLDERS
                and not _credential_name(name)
                and not _other_agent(name)
                and a_tools(folder, name)
                and not _apart(folder, apart)
                and _acceptable(folder)
            ):
                read.append(folder)
        # Where the Visual Studio installer records what is installed.
        # Compilers ask it for the linker and the SDK.
        installed = program_data / "Microsoft" / "VisualStudio"
        if not _apart(installed, apart) and _acceptable(installed):
            read.append(installed)
    return HomeAccess(read, deny)
