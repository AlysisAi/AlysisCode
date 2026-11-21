"""The optional model judge: fixed prompt files, checked against their recorded hashes.

The judge is any OpenAI-compatible chat endpoint the person running the benchmark chooses
(``--judge-url``, ``--judge-key``, ``--judge-model``, or the ALYSIS_OWLS_BENCH_JUDGE_*
environment variables). Its prompt files live in ``judge/`` with a ``manifest.json`` of
their sha256; the scorer refuses to judge when any file differs from the manifest, so
scores from different prompt versions are never mixed by accident.

After a deliberate, reviewed change to a prompt file, record its new hash with
``python3 -m scripts.qa.owls_bench.judge --write-manifest``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

JUDGE_DIR = Path(__file__).resolve().parent / "judge"
MANIFEST = "manifest.json"
REPLY_LIMIT = 8_000
FILES_LIMIT = 16_000
RECEIPTS_LIMIT = 6_000


class JudgeIntegrityError(RuntimeError):
    """The judge's prompt files differ from the hashes recorded in its manifest."""


@dataclass(frozen=True)
class JudgePrompts:
    system: str
    request: str
    rubric: dict
    digest: str


@dataclass(frozen=True)
class JudgeConfig:
    url: str
    key: str
    model: str
    timeout_s: float = 180.0
    temperature: float | None = 0.0
    json_mode: bool = False

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("The judge URL must be an http(s) address.")
        if not self.model.strip():
            raise ValueError("Name the judge model with --judge-model.")

    def describe(self) -> dict:
        """What a report may record about the judge: never the key."""
        parts = urlsplit(self.url)
        return {"endpoint_host": parts.hostname, "model": self.model}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompt_files(folder: Path = JUDGE_DIR) -> dict[str, str]:
    """The sha256 of every judge file but the manifest itself."""
    return {
        path.name: _sha256(path)
        for path in sorted(folder.iterdir())
        if path.is_file() and path.name != MANIFEST
    }


def write_manifest(folder: Path = JUDGE_DIR) -> dict:
    manifest = {"schema": 1, "files": prompt_files(folder)}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(folder: Path = JUDGE_DIR) -> JudgePrompts:
    """The judge prompts, only if every file matches the manifest exactly."""
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JudgeIntegrityError(f"The judge manifest cannot be read: {exc}") from exc
    recorded = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(recorded, dict) or not recorded:
        raise JudgeIntegrityError("The judge manifest lists no files.")
    actual = prompt_files(folder)
    problems = []
    for name in sorted(set(recorded) | set(actual)):
        if name not in actual:
            problems.append(f"{name} is listed in the manifest but missing")
        elif name not in recorded:
            problems.append(f"{name} is not listed in the manifest")
        elif actual[name] != recorded[name]:
            problems.append(f"{name} changed since its hash was recorded")
    if problems:
        raise JudgeIntegrityError(
            "Refusing to judge: " + "; ".join(problems) + ". Review the change, then record it "
            "with `python3 -m scripts.qa.owls_bench.judge --write-manifest`."
        )
    for required in ("system.md", "request.md", "rubric.json"):
        if required not in actual:
            raise JudgeIntegrityError(f"The judge needs {required}.")
    rubric = json.loads((folder / "rubric.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(
        json.dumps(recorded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return JudgePrompts(
        system=(folder / "system.md").read_text(encoding="utf-8"),
        request=(folder / "request.md").read_text(encoding="utf-8"),
        rubric=rubric,
        digest=digest,
    )


def rubric_items(prompts: JudgePrompts, category: str) -> list[dict]:
    items = list(prompts.rubric.get("common", []))
    items += prompts.rubric.get("categories", {}).get(category, [])
    seen: set[str] = set()
    unique = []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique


def _data(text: str, limit: int) -> str:
    """Run data for the judge: bounded, and unable to close its own data block."""
    text = (text or "").replace("<<<", "‹‹‹")
    if len(text) > limit:
        text = text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"
    return text


def build_messages(
    prompts: JudgePrompts,
    *,
    task: dict,
    category: str,
    reply: str,
    files_text: str,
    receipts: dict,
) -> list[dict]:
    rubric = "\n".join(
        f"- {item['id']}: {item['text']}" for item in rubric_items(prompts, category)
    )
    values = {
        "{{task}}": _data(json.dumps(task, ensure_ascii=False, indent=2), 8_000),
        "{{rubric}}": rubric,
        "{{reply}}": _data(reply, REPLY_LIMIT),
        "{{files}}": _data(files_text or "(no files were delivered)", FILES_LIMIT),
        "{{receipts}}": _data(json.dumps(receipts, ensure_ascii=False, indent=2), RECEIPTS_LIMIT),
    }
    request = re.sub(
        r"\{\{(task|rubric|reply|files|receipts)\}\}",
        lambda match: values[match.group(0)],
        prompts.request,
    )
    return [
        {"role": "system", "content": prompts.system},
        {"role": "user", "content": request},
    ]


def endpoint(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def request_verdict(
    config: JudgeConfig,
    messages: list[dict],
    *,
    opener: Callable | None = None,
) -> str:
    """The judge's answer text. Raises OSError or ValueError when the call fails."""
    body: dict = {"model": config.model, "messages": messages}
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.json_mode:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        endpoint(config.url),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.key}",
        },
        method="POST",
    )
    with (opener or urllib.request.urlopen)(request, timeout=config.timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise ValueError("The judge returned no choices.")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("The judge returned an empty answer.")
    return content


def _json_objects(text: str):
    """Each balanced {...} block in the text, outermost first, as parsed JSON."""
    depth, start, in_string, escaped = 0, None, False, False
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"' and depth:
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = index
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    yield json.loads(text[start : index + 1])
                except ValueError:
                    pass
                start = None


def parse_verdict(content: str, item_ids: list[str], *, scale_max: int = 2) -> dict:
    """Per-item scores (0..1) and reasons from the judge's answer, read defensively.

    Unknown items are ignored, missing ones stay None, scores are clamped to the scale, and
    answers wrapped in prose or code fences still parse. The run's judge score is the mean
    of the items that have a score.
    """
    text = re.sub(r"```(?:json)?", "", content or "", flags=re.IGNORECASE)
    raw_items = None
    for candidate in _json_objects(text):
        if isinstance(candidate, dict) and isinstance(candidate.get("items"), list):
            raw_items = candidate["items"]
            break
        if isinstance(candidate, dict) and any(key in item_ids for key in candidate):
            raw_items = [
                {"id": key, **(value if isinstance(value, dict) else {"score": value})}
                for key, value in candidate.items()
            ]
            break
    items = {item_id: {"score": None, "reason": ""} for item_id in item_ids}
    if raw_items is None:
        return {"items": items, "score": None, "parse_error": "no rubric JSON in the answer"}
    for raw in raw_items:
        if not isinstance(raw, dict) or raw.get("id") not in items:
            continue
        score = raw.get("score")
        if isinstance(score, str):
            try:
                score = float(score.strip())
            except ValueError:
                score = None
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = None
        if score is not None:
            score = min(max(float(score), 0.0), float(scale_max)) / scale_max
        reason = raw.get("reason")
        items[raw["id"]] = {
            "score": score,
            "reason": reason.strip()[:400] if isinstance(reason, str) else "",
        }
    scored = [item["score"] for item in items.values() if item["score"] is not None]
    return {
        "items": items,
        "score": round(sum(scored) / len(scored), 4) if scored else None,
        "parse_error": None if scored else "no rubric item had a score",
    }


def judge_run(
    prompts: JudgePrompts,
    config: JudgeConfig,
    *,
    task: dict,
    category: str,
    reply: str,
    files_text: str,
    receipts: dict,
    opener: Callable | None = None,
) -> dict:
    """Ask the judge about one run. A failed call is recorded as an error, never as a score."""
    messages = build_messages(
        prompts, task=task, category=category, reply=reply, files_text=files_text, receipts=receipts
    )
    item_ids = [item["id"] for item in rubric_items(prompts, category)]
    scale_max = int(prompts.rubric.get("scale", {}).get("max", 2))
    try:
        content = request_verdict(config, messages, opener=opener)
    except urllib.error.HTTPError as exc:
        return {"status": "error", "error": f"the judge endpoint answered HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": f"the judge call failed: {type(exc).__name__}"}
    verdict = parse_verdict(content, item_ids, scale_max=scale_max)
    return {
        "status": "ok" if verdict["score"] is not None else "unparsed",
        "digest": prompts.digest,
        **verdict,
    }


def config_from(args) -> JudgeConfig | None:
    """The judge chosen on the command line or in the environment, or None for no judge."""
    url = args.judge_url or os.environ.get("ALYSIS_OWLS_BENCH_JUDGE_URL")
    key = args.judge_key or os.environ.get("ALYSIS_OWLS_BENCH_JUDGE_KEY", "")
    model = args.judge_model or os.environ.get("ALYSIS_OWLS_BENCH_JUDGE_MODEL", "")
    if getattr(args, "no_judge", False) or not url:
        return None
    return JudgeConfig(
        url=url,
        key=key,
        model=model,
        timeout_s=args.judge_timeout,
        temperature=None if args.judge_no_temperature else 0.0,
        json_mode=args.judge_json_mode,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("judge (optional)")
    group.add_argument(
        "--judge-url", help="OpenAI-compatible base URL (env ALYSIS_OWLS_BENCH_JUDGE_URL)"
    )
    group.add_argument("--judge-key", help="API key (prefer env ALYSIS_OWLS_BENCH_JUDGE_KEY)")
    group.add_argument("--judge-model", help="model id (env ALYSIS_OWLS_BENCH_JUDGE_MODEL)")
    group.add_argument("--judge-timeout", type=float, default=180.0, help="seconds per judge call")
    group.add_argument(
        "--judge-no-temperature",
        action="store_true",
        help="do not send temperature 0 (for endpoints that reject it)",
    )
    group.add_argument(
        "--judge-json-mode",
        action="store_true",
        help="ask the endpoint for a JSON object response, if it supports that",
    )
    group.add_argument("--no-judge", action="store_true", help="skip the judge even if configured")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or record the judge prompt hashes.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="verify the prompt files")
    action.add_argument(
        "--write-manifest",
        action="store_true",
        help="record the current prompt files' hashes after a reviewed change",
    )
    parser.add_argument("--folder", type=Path, default=JUDGE_DIR)
    args = parser.parse_args(argv)
    if args.write_manifest:
        manifest = write_manifest(args.folder)
        print(f"Recorded {len(manifest['files'])} judge files.")
        return 0
    try:
        prompts = verify(args.folder)
    except JudgeIntegrityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Judge prompts match their manifest (digest {prompts.digest[:12]}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
