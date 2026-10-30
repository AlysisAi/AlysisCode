"""Private main-owned GitHub repository policy and reviewed finite route extractors.

This module is not an account authority. Main must revalidate its live binding at
its actual transport; the engine binds a closed snapshot and rejects unsupported
requests before preparation. Nothing here may select a human repository entry.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ..host_actions import HostActionError

NATIVE = {
    "connectors.status": (
        "connection_status",
        "867a5b37aeb6d55a0985b3193ca46df1894f2ee0ccc1ac4ba6050aeb687a8e72",
    ),
    "connectors.github.repositories": (
        "github_repositories",
        "2974d644b2cb73f8e01fed6a0e0bb34c735ba36cf648c52319e497c3cc926b7d",
    ),
    "github.repo.read": (
        "github_repo_read",
        "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    ),
    "github.pr.list": (
        "github_pr_list",
        "15f91b49dc8154fa18ae84c76a4349800f8e125b1ab65dd4d9146095ccfa61cd",
    ),
    "github.pr.create": (
        "github_pr_create",
        "98927831a6031b8d826c1914c6d2ff8b903cf42a0724c4f05c9f89242cd1ecc5",
    ),
    "github.git.fetch": (
        "github_git_fetch",
        "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    ),
    "github.git.pull": (
        "github_git_pull",
        "a1463931e29e5d37ea8a1e189b0298a6d9281b5ee3545eedc5279ac7ff82768f",
    ),
    "github.git.push": (
        "github_git_push",
        "b331992688eac76074a29f7707f1f5956e664f03e6a2676f82b1d2fb5b840b64",
    ),
}
EFFECTIVE_PINS = {
    "github_api_read": "9633cf1358967ed8340d30a8c9d0702bc25d5de5f5b3810b5b1dee53e7f58938",
    "github_api_write": "b79685ba44297d555c4114b3a6deeeb7dda604211d70c28e630102806aaa693a",
}
REPOSITORY_ACTIONS = frozenset(NATIVE) - {"connectors.status"} | {
    "mcp.github.discover",
    "mcp.github.describe",
    "mcp.github.call",
}
OWNER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?\Z")
REPO = re.compile(r"[a-z0-9_.-]{1,100}\Z")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
WHITESPACE = frozenset(range(33)) | {
    0x85,
    0xA0,
    0x1680,
    *range(0x2000, 0x200B),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
    0xFEFF,
}


def fail() -> NoReturn:
    # Fixed diagnostic: no policy, account, argument, or provider content leaks.
    raise HostActionError(
        "github_repository_policy_denied",
        "GitHub repository access is unavailable under the current plugin policy.",
        dispatch_state="not_dispatched",
    )


def json_copy(value: Any, limit: int = 65536) -> Any:
    def walk(v: Any, depth: int = 0) -> None:
        if depth > 32:
            fail()
        if type(v) is dict:
            for k, item in v.items():
                if type(k) is not str:
                    fail()
                walk(item, depth + 1)
        elif type(v) is list:
            for item in v:
                walk(item, depth + 1)
        elif type(v) not in (str, int, float, bool, type(None)):
            fail()

    try:
        walk(value)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > limit:
            fail()
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        fail()


def schema_digest(value: Any) -> str:
    value = json_copy(value)
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def repository(value: Any) -> tuple[str, str]:
    if type(value) is not dict or set(value) != {"owner", "repo"}:
        fail()
    owner, repo = value["owner"], value["repo"]
    if (
        type(owner) is not str
        or not OWNER.fullmatch(owner)
        or type(repo) is not str
        or not REPO.fullmatch(repo)
        or repo in {".", ".."}
    ):
        fail()
    return owner, repo


@dataclass(frozen=True)
class RepositoryPolicy:
    plugin_manifest_sha256: str
    account_login: str
    account_link_revision: str
    policy_revision: str
    mode: str
    repositories: tuple[tuple[str, str], ...]
    workspace_repository: tuple[str, str] | None

    @classmethod
    def parse(cls, value: Any) -> RepositoryPolicy:
        value = json_copy(value)
        if type(value) is not dict or set(value) != {
            "version",
            "plugin_id",
            "plugin_manifest_sha256",
            "host",
            "account_login",
            "account_link_revision",
            "policy_revision",
            "mode",
            "repositories",
            "workspace_repository",
        }:
            fail()
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["plugin_id"] != "alysis.github"
            or value["host"] != "github.com"
        ):
            fail()
        for key, pattern in [
            ("plugin_manifest_sha256", re.compile(r"[a-f0-9]{64}\Z")),
            ("account_login", OWNER),
            ("account_link_revision", UUID),
            ("policy_revision", UUID),
        ]:
            if type(value[key]) is not str or not pattern.fullmatch(value[key]):
                fail()
        if value["mode"] not in ("all", "allow_list") or type(value["repositories"]) is not list:
            fail()
        repos = tuple(repository(r) for r in value["repositories"])
        if (
            len(repos) > 256
            or repos != tuple(sorted(set(repos), key=lambda item: "/".join(item)))
            or value["mode"] == "all"
            and repos
        ):
            fail()
        workspace = (
            None
            if value["workspace_repository"] is None
            else repository(value["workspace_repository"])
        )
        return cls(
            value["plugin_manifest_sha256"],
            value["account_login"],
            value["account_link_revision"],
            value["policy_revision"],
            value["mode"],
            repos,
            workspace,
        )

    def payload(self) -> dict[str, Any]:
        return dict(
            version=1,
            plugin_id="alysis.github",
            host="github.com",
            plugin_manifest_sha256=self.plugin_manifest_sha256,
            account_login=self.account_login,
            account_link_revision=self.account_link_revision,
            policy_revision=self.policy_revision,
            mode=self.mode,
            repositories=[dict(owner=o, repo=r) for o, r in self.repositories],
            workspace_repository=None
            if self.workspace_repository is None
            else dict(zip(("owner", "repo"), self.workspace_repository, strict=True)),
        )

    def assert_plugin_current(self, root: Path) -> None:
        from .github_host import active_github_plugin

        plugin = active_github_plugin(root)
        if plugin is None or plugin.get("manifest_sha256") != self.plugin_manifest_sha256:
            fail()

    def target(self, value: Any) -> tuple[str, str]:
        if type(value) is not dict:
            fail()
        if value == {"scope": "workspace"}:
            result = self.workspace_repository
        elif set(value) == {"scope", "owner", "repo"} and value["scope"] == "repository":
            result = repository({k: value[k] for k in ("owner", "repo")})
        else:
            fail()
        if result is None or self.mode == "allow_list" and result not in self.repositories:
            fail()
        return result


def check_native_schema(action: str) -> None:
    from ..tools.registry import copied_tool_parameters

    name, pin = NATIVE[action]
    if schema_digest(copied_tool_parameters(name)) != pin:
        fail()


def check_definition(tool: dict[str, Any]) -> None:
    name = tool.get("name")
    if (
        type(name) is not str
        or name not in EFFECTIVE_PINS
        or tool.get("scope") != "repository"
        or type(tool.get("readOnly")) is not bool
        or tool["readOnly"] != (name == "github_api_read")
    ):
        fail()
    if "inputSchema" in tool and schema_digest(tool["inputSchema"]) != EFFECTIVE_PINS[name]:
        fail()


def text(value: Any, limit: int, *, blank: bool = False) -> str:
    if type(value) is not str:
        fail()
    try:
        units = len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        fail()
    if (
        units > limit
        or not blank
        and not any(ord(c) not in WHITESPACE for c in value)
        or any(ord(c) < 32 and c not in "\t\n\r" or ord(c) == 127 for c in value)
    ):
        fail()
    return value


def branch(value: Any) -> str:
    value = text(value, 256)
    if (
        any(ord(c) in WHITESPACE or c in "~^:?*[\\" for c in value)
        or any(s in value for s in ("..", "@{", "//"))
        or value == "@"
        or value.startswith(("-", "+", "/"))
        or value.endswith(("/", "."))
        or any(p.startswith(".") or p.endswith(".lock") for p in value.split("/"))
    ):
        fail()
    return value


def closed(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    if (
        type(value) is not dict
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        fail()


def pr(value: Any) -> dict[str, Any]:
    closed(value, {"base", "head", "title", "body"}, {"draft"})
    draft = value.get("draft", True)
    if type(draft) is not bool:
        fail()
    return dict(
        base=branch(value["base"]),
        head=branch(value["head"]),
        title=text(value["title"], 256),
        body=text(value["body"], 4096, blank=True),
        draft=draft,
    )


def normalize_arguments(name: str, arguments: Any) -> dict[str, Any]:
    args = json_copy(arguments, 16384)
    if name in {"github.repo.read", "github.git.fetch"}:
        closed(args, set())
    elif name == "connectors.status":
        closed(args, {"connector"})
        if args["connector"] != "github":
            fail()
    elif name == "connectors.github.repositories":
        closed(args, set(), {"page"})
        page = args.get("page", 1)
        if type(page) is not int or not 1 <= page <= 10000:
            fail()
        return {"page": page}
    elif name == "github.pr.list":
        closed(args, set(), {"base", "head"})
        return {k: branch(v) for k, v in args.items()}
    elif name == "github.pr.create":
        return pr(args)
    elif name == "github.git.pull":
        closed(args, {"branch"})
        return {"branch": branch(args["branch"])}
    elif name == "github.git.push":
        closed(args, {"branch"}, {"expected_remote_head"})
        result = {"branch": branch(args["branch"])}
        if "expected_remote_head" in args:
            head = args["expected_remote_head"]
            if (
                type(head) is not str
                or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", head) is None
            ):
                fail()
            result["expected_remote_head"] = head
        return result
    elif name == "github_api_read":
        closed(args, {"path"}, {"query"})
        if args["path"] == "":
            closed(args, {"path"})
        elif args["path"] == "branches":
            query = args.get("query", {})
            closed(query, set(), {"page", "per_page"})
            values = {}
            for key, default, bound in [("page", "1", 10000), ("per_page", "100", 100)]:
                value = query.get(key, default)
                if (
                    type(value) is not str
                    or not re.fullmatch(r"[1-9][0-9]{0,4}", value)
                    or int(value) > bound
                ):
                    fail()
                values[key] = value
            return {"path": "branches", "query": values}
        else:
            fail()
    elif name == "github_api_write":
        closed(args, {"path", "method", "body"})
        if args["path"] != "pulls" or args["method"] != "POST":
            fail()
        return {"path": "pulls", "method": "POST", "body": pr(args["body"])}
    else:
        fail()
    return args


def guard_arguments(
    policy: RepositoryPolicy | None, action: str, args: dict[str, Any], state: Any
) -> dict[str, Any]:
    if action not in REPOSITORY_ACTIONS and not (
        action == "connectors.status" and args.get("connector") == "github"
    ):
        return args
    if action == "connectors.status" and policy is None:
        return args
    if policy is None:
        fail()
    if policy.mode == "all":
        return args
    if action.startswith("mcp.github."):
        from .github_discovery import discovery_arguments

        args = discovery_arguments(action, args)
    if action in NATIVE:
        check_native_schema(action)
        if action.startswith("github."):
            policy.target({"scope": "workspace"})
        return normalize_arguments(action, args)
    if action == "mcp.github.discover":
        if "query" in args:
            if len(args["query"].encode("utf-16-le")) // 2 > 256:
                fail()
        return args
    if action == "mcp.github.describe":
        if any(n not in EFFECTIVE_PINS for n in args["names"]):
            fail()
        policy.target(args["target"])
        return args
    if action == "mcp.github.call":
        tool = state.loaded.get((args.get("target_id"), args.get("name")))
        if tool is None:
            fail()
        check_definition(tool)
        policy.target(state.targets.get(args["target_id"]))
        return {**args, "arguments": normalize_arguments(args["name"], args["arguments"])}
    fail()


def guard_result(
    policy: RepositoryPolicy | None,
    action: str,
    result: dict[str, Any],
    arguments: dict[str, Any] | None = None,
) -> None:
    if policy is None or policy.mode == "all":
        return
    if action in {"mcp.github.discover", "mcp.github.describe"}:
        for tool in result["tools"]:
            check_definition(tool)

    if action.startswith("github."):
        expected = policy.target({"scope": "workspace"})
        if (
            result.get("repository", "").lower() != "/".join(expected)
            or result.get("account_login", "").lower() != policy.account_login
        ):
            fail()
    if action == "connectors.github.repositories":
        page = (arguments or {}).get("page", 1)
        expected = policy.repositories[(page - 1) * 100 : page * 100]
        rows = result.get("repositories", [])
        names = [r["name"].lower() for r in rows]
        next_page = page + 1 if page * 100 < len(policy.repositories) else None
        if (
            result.get("account", "").lower() != policy.account_login
            or names != ["/".join(r) for r in expected]
            or result.get("next_page") != next_page
        ):
            fail()
