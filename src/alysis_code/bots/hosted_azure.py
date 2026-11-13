"""Azure controller with an explicit pilot deadline or continuous hosting.

Managed identity handles storage and task computers. Model/API credentials
arrive over stdin and stay in RAM. Optional TLS retains certificate validation;
network ingress is separately restricted by the reviewed Azure NSG. Host units
use hosted_operations to retrieve the pinned bootstrap secret after a reboot.
Computer demand queues behind the existing shared capacity reservation.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import secrets
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID

import uvicorn

from .app import Settings, create_app
from .cloud_desktop import CloudDesktopFactory
from .compute import ComputerSpec
from .compute_azure import TOKEN_SCOPE, AzureComputers
from .object_storage_azure import AzureBlobStorage
from .postgres import PostgresStore
from .serving import serve


@dataclass(frozen=True)
class HostedConfig:
    state_directory: Path
    database_dsn: str = field(repr=False)
    storage_url: str
    storage_container: str
    subscription: str
    resource_group: str
    sandbox_group: str
    sandbox_region: str
    worker_image: str
    expires_at: float | None
    browser_state_key: str = field(repr=False)
    port: int = 7080
    max_computers: int | None = 2
    account_library_bytes: int | None = None
    managed_cloud: bool = False
    max_accounts: int = 8
    tls_certificate: str | None = field(default=None, repr=False)
    tls_private_key: str | None = field(default=None, repr=False)
    computer_stream_url: str | None = None
    computer_stream_bots: tuple[str, ...] | None = None
    billing_url: str | None = None
    billing_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.computer_stream_url is not None:
            from .computer_stream import validate_url

            validate_url(self.computer_stream_url)
        if self.computer_stream_bots is not None:
            for bot_id in self.computer_stream_bots:
                if not isinstance(bot_id, str) or UUID(bot_id).hex != bot_id:
                    raise ValueError("Use canonical Bot IDs for a private stream canary.")
        try:
            valid_key = len(base64.b64decode(self.browser_state_key, validate=True)) == 32
        except (ValueError, TypeError):
            valid_key = False
        if not valid_key:
            raise ValueError("A stable base64-encoded 32-byte browser storage key is required")
        if not self.state_directory.is_absolute():
            raise ValueError("Hosted scratch directory must be absolute")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("Invalid loopback service port")
        if self.max_computers is not None and (
            type(self.max_computers) is not int or self.max_computers < 1
        ):
            raise ValueError("Computer capacity must be positive or explicit null")
        if self.account_library_bytes is not None and (
            type(self.account_library_bytes) is not int
            or not 0 <= self.account_library_bytes <= 2**63 - 1
        ):
            raise ValueError("Account Library allowance must be nonnegative bytes or null")
        if (
            type(self.managed_cloud) is not bool
            or type(self.max_accounts) is not int
            or not 1 <= self.max_accounts <= 32
        ):
            raise ValueError("Use an explicit managed Cloud mode and a bounded account capacity")
        if self.expires_at is not None and (
            type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at)
        ):
            raise ValueError("Use a finite pilot expiry or explicit null for continuous hosting")
        if (self.tls_certificate is None) != (self.tls_private_key is None) or any(
            not isinstance(value, str) or not 1 <= len(value) <= 8192
            for value in (self.tls_certificate, self.tls_private_key)
            if value is not None
        ):
            raise ValueError("Provide both the public TLS certificate and private key")


def build(settings, hosted, credential, resources):
    """Credential injection also permits explicit isolated operator qualification."""
    if settings.database.parent.resolve() != hosted.state_directory.resolve():
        raise ValueError("Use the hosted scratch directory for local connection state")
    billing = None
    if hosted.managed_cloud:
        from .cloud_billing import CloudBilling

        if not hosted.billing_url or not hosted.billing_key:
            raise ValueError("Managed Cloud requires its controller billing connection.")
        billing = CloudBilling(hosted.billing_url, hosted.billing_key)
        resources.callback(billing.close)
    storage = AzureBlobStorage(hosted.storage_url, hosted.storage_container, credential)
    resources.callback(storage.close)
    shared = PostgresStore(
        hosted.database_dsn, objects=storage, state_directory=hosted.state_directory
    )
    resources.callback(shared.close)
    provider = AzureComputers(
        subscription=hosted.subscription,
        resource_group=hosted.resource_group,
        sandbox_group=hosted.sandbox_group,
        region=hosted.sandbox_region,
        access_token=lambda: credential.get_token(TOKEN_SCOPE).token,
        expires_at=hosted.expires_at,
    )
    resources.callback(provider.close)
    browser_key = base64.b64decode(hosted.browser_state_key, validate=True)
    streams = None
    if hosted.computer_stream_url:
        from .computer_stream import ComputerStreams

        streams = ComputerStreams(
            hosted.computer_stream_url,
            certificate=hosted.tls_certificate,
            bots=hosted.computer_stream_bots,
        )
        resources.callback(streams.close)

    def account_app(config, *, allowed=None):
        account_billing = (
            billing.account(config.owner, shared)
            if billing and config.owner.startswith("account:")
            else None
        )
        factory = CloudDesktopFactory(
            config.owner,
            provider,
            ComputerSpec(
                hosted.worker_image,
                cpu=2,
                memory_mib=4096,
                idle_seconds=180,
                internet=True,
                suspend_mode="Memory",
            ),
            config.database.parent / "computer-leases",
            browser_enabled=True,
            persist_workspace=True,
            store=shared,
            browser_state_key=browser_key,
            max_computers=hosted.max_computers,
            wait_for_capacity=True,
            computer_streams=streams,
            billing=account_billing,
        )
        app = create_app(
            replace(config, service_mode="cloud"),
            task_store=shared,
            cloud_computers=factory,
            work_authorizer=allowed,
        )
        app.state.account_token = config.token
        return app

    operator = account_app(settings)
    if streams:
        operator.mount("/internal/computer-stream", streams)
    if not hosted.managed_cloud:
        return operator
    from .managed_cloud import AccountApplications, AccountAuthority

    authority = AccountAuthority(shared, browser_key)

    def create_account(owner):
        billing.account(owner, shared).status()
        config = replace(
            settings,
            owner=owner,
            token=secrets.token_hex(32),
            database=hosted.state_directory / "accounts" / owner[8:] / "local-config",
            coding_token=None,
            managed_workspaces=False,
        )
        return account_app(config, allowed=lambda: authority.allowed(owner))

    return AccountApplications(
        operator, authority, create_account, max_accounts=hosted.max_accounts
    ).application()


def bootstrap(stream):
    raw = stream.readline(16385)
    if len(raw) > 16384:
        raise ValueError("Hosted bootstrap exceeds its allowance")
    values = json.loads(raw)
    hosted_values = dict(values["hosted"])
    # The public controller origin is not a credential. Operators may configure
    # it in the host unit without rotating or copying the private bootstrap.
    if stream_url := os.environ.get("ALYSIS_BOTS_COMPUTER_STREAM_URL"):
        hosted_values["computer_stream_url"] = stream_url
    if stream_bots := os.environ.get("ALYSIS_BOTS_COMPUTER_STREAM_BOTS"):
        hosted_values["computer_stream_bots"] = tuple(stream_bots.split(","))
    hosted = HostedConfig(
        **{**hosted_values, "state_directory": Path(hosted_values["state_directory"])}
    )
    settings = Settings(database=hosted.state_directory / "local-config", **values["settings"])
    return settings, hosted


def main():
    from azure.identity import ManagedIdentityCredential

    try:
        settings, hosted = bootstrap(sys.stdin.buffer)
        with ExitStack() as resources:
            credential = resources.enter_context(ManagedIdentityCredential())
            app = build(settings, hosted, credential, resources)
            from .hosted_tls import server_config

            server = uvicorn.Server(server_config(app, hosted))
            asyncio.run(serve(server, app.state.runtime))
    except Exception:
        # Upstream errors/configuration can contain credentials. The operator
        # inspects service health and sanitized receipts, not raw bootstrap data.
        raise SystemExit("Hosted Bots stopped; inspect the private pilot health receipt.") from None


if __name__ == "__main__":
    main()
