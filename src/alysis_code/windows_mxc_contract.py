"""Side-effect-free compatibility contract; never a claim of host or release readiness."""

UPSTREAM_COMMIT = "c6f301d53a1430c4c921a05c57af838f7392348f"
WINDOWS_RUNTIME_DIRECTORIES = (
    "System32",
    "Microsoft.NET",
    "WinSxS",
    "assembly",
    "Fonts",
    "Globalization",
)


def helper_contract() -> dict:
    return {
        "protocol": 4,
        "security_revision": 11,
        "implementation": "alysis-windows-sandbox",
        "containment": "mxc-psec",
        "upstream_commit": UPSTREAM_COMMIT,
        "network_modes": ["off", "on"],
        "host_acl_mutation": False,
        "automatic_fallback": False,
        "ancestor_grants": False,
        "qualification": "prototype-unqualified",
        "metadata_reservations": "persistent-protected-directories",
        "scoped_local_preview": True,
        "directory_discovery": True,
    }


def engine_capabilities() -> dict:
    return {
        **helper_contract(),
        "contract": "alysis.windows-mxc.capabilities",
        "schema_version": 1,
        "platforms": ["win32-x64"],
        "filesystem_modes": ["read-only", "workspace-write"],
        "features": {"foreground": True, "background": True, "durable_services": False},
        "desktop_activation": "signed-beta-or-local-candidate",
        "system_read_scope": "runtime-directories-v1",
        "private_state_protection": "host-control-paths-v1",
        "host_runtime_protection": "python-installations-v1",
    }
