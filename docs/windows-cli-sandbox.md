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
this general-purpose interpreter. Other tools installed outside the workspace need explicit `windows_read_roots` when
the selected command cannot otherwise read them. There is no broad automatic
grant over the user's PATH or home directory. Durable services remain unavailable
with this backend; use session-owned background commands.

## Release maintenance

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
