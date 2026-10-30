"""Shared path-only credential classification, independent of filesystem tools."""

from __future__ import annotations

import os

_SAFE_ENV_TEMPLATE_SUFFIXES = (
    ".example",
    ".sample",
    ".template",
    ".dist",
    ".defaults",
)


_PRIVATE_KEY_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".ppk", ".jks", ".keystore")


_PRIVATE_KEY_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}


_CREDENTIAL_FILE_NAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
    "auth.json",
    "credentials",
    "credentials.json",
    "credentials.tfrc.json",
    "dockerconfigjson",
    "kubeconfig",
    "netrc",
    "service-account.json",
    "service_account.json",
}


_SENSITIVE_DIRECTORY_NAMES = {
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".gnupg",
    ".kube",
    ".ssh",
    ".alysis",
}


def sensitive_path_category(path: str | os.PathLike[str]) -> str | None:
    """Classify credential-bearing paths without opening or inspecting their content."""

    normalized = os.fspath(path).replace("\\", "/").strip("/")
    parts = [part.casefold() for part in normalized.split("/") if part]
    if not parts:
        return None
    name = parts[-1]

    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in parts):
        return "credential_directory"

    if name == ".env" or name.startswith(".env."):
        if name.endswith(_SAFE_ENV_TEMPLATE_SUFFIXES):
            return None
        return "environment_file"

    if name in _PRIVATE_KEY_NAMES or (
        name.endswith(_PRIVATE_KEY_SUFFIXES) and not name.endswith(".pub")
    ):
        return "private_key"

    if name in _CREDENTIAL_FILE_NAMES:
        return "credential_file"
    if name.startswith(("service-account-", "service_account_")) and name.endswith(".json"):
        return "credential_file"
    if name.startswith(("secret.", "secrets.")) or name in {
        "secret",
        "secrets",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }:
        return "secret_file"
    if len(parts) >= 2 and parts[-2:] == [".aws", "credentials"]:
        return "credential_file"
    if len(parts) >= 2 and parts[-2:] == [".docker", "config.json"]:
        return "credential_file"
    if len(parts) >= 2 and parts[-2:] == [".azure", "accesstokens.json"]:
        return "credential_file"
    return None
