from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Brackets can be legitimate URL path/query characters, such as ?foo[bar]=1.
# Surrounding wrapper brackets are removed later by context-aware cleanup.
_URL_RE = re.compile(r"https?://[^\s<>{}\"]+", re.IGNORECASE)
_URL_CANDIDATE_STOP_CHARS = set('<>{}"')
_STRUCTURED_URL_FORBIDDEN_CHARS = set('<>{}"\\')
_SIMPLE_TRAILING_URL_PUNCTUATION = ".,;:?"
_TRAILING_URL_QUOTES = '"'
_URL_QUOTE_WRAPPERS = {"'", '"'}
_URL_WRAPPER_PAIRS = {
    "(": ")",
    "[": "]",
    "{": "}",
    '"': '"',
    "'": "'",
}
_URL_CLOSING_WRAPPERS = {")": "(", "]": "[", "}": "{"}
_MARKDOWN_URL_WRAPPER_MARKERS = ("`", "*", "_")
_USER_PROVIDED = "user_provided"
_RETURNED_BY_WEB_SEARCH = "returned_by_web_search"
_CANONICAL_REDIRECT = "canonical_redirect"
_SEARCH_MEDIATED_RECOVERY = "search_mediated_recovery"
_SAME_ORIGIN_DERIVED = "same_origin_derived_search_result"
_FETCHED_PAGE_LINK = "fetched_page_link"
_TRUSTED_LOCAL_FILE = "trusted_local_file"
_TRUSTED_TOOL_OUTPUT = "trusted_tool_output"
_TRUSTED_DOMAIN_ALLOWLIST = "trusted_domain_allowlist"
_MAX_PROVENANCE_NODES = 512
_MAX_URLS_PER_EVENT = 24
_MAX_URL_TEXT_CHARS = 32_000
_MAX_PROVENANCE_URL_LENGTH = 2048
_MAX_RESULT_TEXT_NODES = 256
_MAX_RESULT_TEXT_DEPTH = 12
_FETCHABLE_PROVENANCE_CLASSES = {
    _USER_PROVIDED,
    _RETURNED_BY_WEB_SEARCH,
    _FETCHED_PAGE_LINK,
    _TRUSTED_LOCAL_FILE,
    _TRUSTED_TOOL_OUTPUT,
    _CANONICAL_REDIRECT,
    _SEARCH_MEDIATED_RECOVERY,
    _SAME_ORIGIN_DERIVED,
}

# Ordered, model-facing list mirrored into web_fetch rejection payloads. Keep in
# sync with _FETCHABLE_PROVENANCE_CLASSES plus the configured-allowlist class,
# which is fetchable only when the session's tracker has trusted domains set.
FETCHABLE_PROVENANCE_CLASSIFICATIONS: tuple[str, ...] = (
    _USER_PROVIDED,
    _RETURNED_BY_WEB_SEARCH,
    _FETCHED_PAGE_LINK,
    _TRUSTED_LOCAL_FILE,
    _TRUSTED_TOOL_OUTPUT,
    _CANONICAL_REDIRECT,
    _SEARCH_MEDIATED_RECOVERY,
    _SAME_ORIGIN_DERIVED,
    _TRUSTED_DOMAIN_ALLOWLIST,
)


def iter_result_text_snippets(value: Any) -> Iterable[str]:
    """Read bounded result data, decoding JSON rather than treating escapes as URLs.

    MCP fences remain in the model-facing result. Removing the host's fence here
    is only for extracting data references; it never promotes source instructions.
    """
    remaining = _MAX_RESULT_TEXT_NODES

    def walk(item: Any, depth: int) -> Iterable[str]:
        nonlocal remaining
        if depth > _MAX_RESULT_TEXT_DEPTH or remaining <= 0:
            return
        remaining -= 1
        if isinstance(item, str):
            text = item.strip()
            begin = "\n--- BEGIN UNTRUSTED MCP TEXT ---\n"
            end = "\n--- END UNTRUSTED MCP TEXT ---\n[/MCP_UNTRUSTED_TEXT]"
            if text.startswith("[MCP_UNTRUSTED_TEXT]\n") and text.endswith(end):
                _, marker, body = text.partition(begin)
                if marker:
                    text = body[: -len(end)]
            if len(text) <= _MAX_URL_TEXT_CHARS and text.startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(text)
                except RecursionError:
                    return
                except ValueError:
                    pass
                else:
                    yield from walk(decoded, depth + 1)
                    return
            if "http://" in text.casefold() or "https://" in text.casefold():
                yield text[:_MAX_URL_TEXT_CHARS]
        elif isinstance(item, dict):
            for child in item.values():
                if remaining <= 0:
                    break
                yield from walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item[:64]:
                if remaining <= 0:
                    break
                yield from walk(child, depth + 1)

    yield from walk(value, 0)


def extract_result_web_urls(value: Any) -> list[dict[str, str]]:
    """Preserve complete structured URL values, including legal trailing punctuation."""
    found: dict[str, dict[str, str]] = {}
    for snippet in iter_result_text_snippets(value):
        normalized = normalize_web_url(snippet)
        candidates = (
            [{"url": snippet, "normalized_url": normalized, "domain": _domain_for_url(normalized)}]
            if normalized is not None
            else extract_public_web_urls(snippet)
        )
        for entry in candidates:
            if len(entry["url"]) <= _MAX_PROVENANCE_URL_LENGTH:
                found.setdefault(entry["normalized_url"], entry)
            if len(found) >= _MAX_URLS_PER_EVENT:
                return list(found.values())
    return list(found.values())


def _domain_for_url(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def normalize_trusted_domain_entry(raw_entry: Any) -> str | None:
    """Normalize one configured trusted-domain entry to a bare lowercase host.

    Accepts plain hosts (``registry.npmjs.org``), leading-wildcard or leading-dot
    forms (``*.npmjs.org``, ``.npmjs.org`` — equivalent to the bare host, since
    matching already covers subdomains), and full URLs
    (``https://registry.npmjs.org/anything``). Schemes, credentials, ports, and
    paths are dropped. Returns ``None`` when no usable host remains.
    """
    text = str(raw_entry or "").strip().lower()
    for prefix in ("*.", "."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if not text:
        return None
    if "://" not in text:
        head = text.split("/", 1)[0]
        if "@" in head:
            return None
        text = f"https://{head}"
    try:
        host = (urlsplit(text).hostname or "").strip().rstrip(".")
    except ValueError:
        return None
    if not host or any(ch.isspace() for ch in host):
        return None
    return host


def _dedupe_ordered(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize_web_query(raw_query: Any) -> str:
    return re.sub(r"\s+", " ", str(raw_query or "").strip()).casefold()


def build_web_fetch_recovery_search_query(raw_url: Any) -> str:
    normalized = normalize_web_url(raw_url) or canonicalize_web_url_input(raw_url)
    if normalized is None:
        return ""
    try:
        split = urlsplit(normalized)
    except ValueError:
        return ""
    host = (split.hostname or "").rstrip(".").lower()
    path_terms = [
        segment for segment in re.split(r"[^A-Za-z0-9]+", split.path or "") if len(segment) >= 2
    ][:4]
    return " ".join([host, *path_terms]).strip()


def normalize_web_url(raw_url: Any) -> str | None:
    text = str(raw_url or "").strip()
    if not text:
        return None
    if any(ch.isspace() or ch in _STRUCTURED_URL_FORBIDDEN_CHARS for ch in text):
        return None
    try:
        split = urlsplit(text)
    except ValueError:
        return None
    scheme = str(split.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None
    if split.username is not None or split.password is not None:
        return None
    hostname = (split.hostname or "").rstrip(".").lower()
    if not hostname:
        return None
    try:
        port = split.port
    except ValueError:
        return None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = split.path or "/"
    return urlunsplit((scheme, netloc, path, split.query, ""))


def _equivalent_fetch_url_variants(normalized_url: str) -> set[str]:
    normalized = normalize_web_url(normalized_url)
    if normalized is None:
        return set()
    variants = {normalized}
    split = urlsplit(normalized)
    if not split.query:
        path = split.path or "/"
        if path == "/":
            variants.add(urlunsplit((split.scheme, split.netloc, "", "", "")))
        elif path.endswith("/"):
            variants.add(urlunsplit((split.scheme, split.netloc, path.rstrip("/"), "", "")))
        else:
            variants.add(urlunsplit((split.scheme, split.netloc, path + "/", "", "")))
    return {variant for variant in variants if normalize_web_url(variant) is not None}


def _same_origin_bounded_derivative(source_url: str, requested_url: str) -> bool:
    source = normalize_web_url(source_url)
    requested = normalize_web_url(requested_url)
    if source is None or requested is None:
        return False
    source_split = urlsplit(source)
    requested_split = urlsplit(requested)
    if (
        source_split.scheme,
        source_split.netloc,
    ) != (
        requested_split.scheme,
        requested_split.netloc,
    ):
        return False
    source_path = source_split.path.rstrip("/")
    requested_path = requested_split.path.rstrip("/")
    if source_path == requested_path:
        return True
    if not source_path or source_path == "/":
        return False
    bounded_pairs = (
        ("/blob/", "/raw/"),
        ("/-/blob/", "/-/raw/"),
    )
    for source_marker, requested_marker in bounded_pairs:
        if source_marker not in source_path:
            continue
        expected = source_path.replace(source_marker, requested_marker, 1)
        if requested_path == expected:
            return True
    return False


def _looks_like_http_url(text: str) -> bool:
    lowered = str(text or "").strip().casefold()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _strip_outer_markdown_url_wrappers(text: str) -> str:
    candidate = str(text or "").strip()
    for marker in _MARKDOWN_URL_WRAPPER_MARKERS:
        leading = len(candidate) - len(candidate.lstrip(marker))
        if leading == 0:
            continue
        trailing = len(candidate) - len(candidate.rstrip(marker))
        if trailing == 0:
            continue
        trim = min(leading, trailing)
        inner = candidate[trim:-trim].strip()
        if _looks_like_http_url(inner):
            return inner
    return candidate


def _looks_like_wrapped_http_url_candidate(text: str) -> bool:
    candidate = str(text or "").strip()
    if _looks_like_http_url(candidate):
        return True
    stripped = _strip_outer_markdown_url_wrappers(candidate)
    return stripped != candidate and _looks_like_http_url(stripped)


def _strip_outer_url_wrappers(text: str) -> str:
    candidate = str(text or "").strip()
    while len(candidate) >= 2:
        opener = candidate[0]
        closer = candidate[-1]
        if _URL_WRAPPER_PAIRS.get(opener) != closer:
            break
        inner = candidate[1:-1].strip()
        if not _looks_like_wrapped_http_url_candidate(inner):
            break
        candidate = inner
    return candidate


def _strip_simple_trailing_url_punctuation(text: str) -> str:
    candidate = str(text or "").strip()
    while candidate:
        tail = candidate[-1]
        if tail in _SIMPLE_TRAILING_URL_PUNCTUATION or tail in _TRAILING_URL_QUOTES:
            candidate = candidate[:-1]
            continue
        break
    return candidate


def _leading_url_wrapper_closer_budget(text: str, *, start: int) -> dict[str, int]:
    budget: dict[str, int] = {}
    index = start - 1
    while index >= 0:
        ch = text[index]
        if ch in _MARKDOWN_URL_WRAPPER_MARKERS:
            index -= 1
            continue
        closer = _URL_WRAPPER_PAIRS.get(ch)
        if closer is None or closer not in _URL_CLOSING_WRAPPERS:
            break
        budget[closer] = budget.get(closer, 0) + 1
        index -= 1
    return budget


def _leading_url_quote_wrapper_budget(text: str, *, start: int) -> dict[str, int]:
    budget: dict[str, int] = {}
    index = start - 1
    while index >= 0:
        ch = text[index]
        if ch in _MARKDOWN_URL_WRAPPER_MARKERS:
            index -= 1
            continue
        if ch not in _URL_QUOTE_WRAPPERS:
            break
        budget[ch] = budget.get(ch, 0) + 1
        index -= 1
    return budget


def _markdown_link_label_start(text: str, *, close_bracket: int) -> int | None:
    depth = 0
    for index in range(close_bracket - 1, -1, -1):
        ch = text[index]
        if ch == "]":
            depth += 1
            continue
        if ch != "[":
            continue
        if depth > 0:
            depth -= 1
            continue
        if "\n" in text[index + 1 : close_bracket]:
            return None
        return index
    return None


def _scan_public_web_url_candidate_end(text: str, *, start: int) -> int:
    end = start
    while end < len(text):
        ch = text[end]
        if ch.isspace() or ch in _URL_CANDIDATE_STOP_CHARS:
            break
        end += 1
    return end


def _strip_bounded_trailing_url_closers(
    text: str,
    *,
    closer_budget: dict[str, int] | None = None,
) -> tuple[str, dict[str, int]]:
    candidate = str(text or "").strip()
    remaining_budget = {
        str(closer): int(count)
        for closer, count in dict(closer_budget or {}).items()
        if int(count) > 0
    }
    while candidate:
        tail = candidate[-1]
        opener = _URL_CLOSING_WRAPPERS.get(tail)
        if opener is None:
            break
        if remaining_budget.get(tail, 0) <= 0:
            break
        if candidate.count(tail) <= candidate.count(opener):
            break
        candidate = candidate[:-1]
        remaining_budget[tail] -= 1
        if remaining_budget[tail] <= 0:
            remaining_budget.pop(tail, None)
    return candidate, remaining_budget


def _strip_bounded_trailing_url_quotes(
    text: str,
    *,
    quote_budget: dict[str, int] | None = None,
) -> tuple[str, dict[str, int]]:
    candidate = str(text or "").strip()
    remaining_budget = {
        str(quote): int(count)
        for quote, count in dict(quote_budget or {}).items()
        if str(quote) in _URL_QUOTE_WRAPPERS and int(count) > 0
    }
    while candidate:
        tail = candidate[-1]
        if tail not in _URL_QUOTE_WRAPPERS:
            break
        if remaining_budget.get(tail, 0) <= 0:
            break
        candidate = candidate[:-1]
        remaining_budget[tail] -= 1
        if remaining_budget[tail] <= 0:
            remaining_budget.pop(tail, None)
    return candidate, remaining_budget


def _trailing_markdown_url_wrapper_marker(
    text: str,
    *,
    closer_budget: dict[str, int] | None = None,
    quote_budget: dict[str, int] | None = None,
) -> str | None:
    candidate = _strip_simple_trailing_url_punctuation(text)
    candidate, _remaining_budget = _strip_bounded_trailing_url_closers(
        candidate,
        closer_budget=closer_budget,
    )
    candidate, _remaining_quote_budget = _strip_bounded_trailing_url_quotes(
        candidate,
        quote_budget=quote_budget,
    )
    for marker in _MARKDOWN_URL_WRAPPER_MARKERS:
        if candidate.endswith(marker):
            return marker
    return None


def cleanup_public_web_url_candidate(
    raw_url: Any,
    *,
    closer_budget: dict[str, int] | None = None,
    quote_budget: dict[str, int] | None = None,
) -> str:
    candidate = str(raw_url or "").strip()
    previous = None
    remaining_closer_budget = dict(closer_budget or {})
    remaining_quote_budget = dict(quote_budget or {})
    while candidate and candidate != previous:
        previous = candidate
        candidate = _strip_outer_url_wrappers(candidate)
        candidate = _strip_outer_markdown_url_wrappers(candidate)
        candidate = _strip_simple_trailing_url_punctuation(candidate)
        candidate, remaining_closer_budget = _strip_bounded_trailing_url_closers(
            candidate,
            closer_budget=remaining_closer_budget,
        )
        candidate, remaining_quote_budget = _strip_bounded_trailing_url_quotes(
            candidate,
            quote_budget=remaining_quote_budget,
        )
    return candidate


def cleanup_structured_web_url_target(
    raw_url: Any,
    *,
    closer_budget: dict[str, int] | None = None,
    quote_budget: dict[str, int] | None = None,
) -> str:
    candidate = str(raw_url or "").strip()
    previous = None
    remaining_closer_budget = dict(closer_budget or {})
    remaining_quote_budget = dict(quote_budget or {})
    while candidate and candidate != previous:
        previous = candidate
        candidate = _strip_outer_url_wrappers(candidate)
        candidate = _strip_outer_markdown_url_wrappers(candidate)
        candidate, remaining_closer_budget = _strip_bounded_trailing_url_closers(
            candidate,
            closer_budget=remaining_closer_budget,
        )
        candidate, remaining_quote_budget = _strip_bounded_trailing_url_quotes(
            candidate,
            quote_budget=remaining_quote_budget,
        )
    return candidate


def canonicalize_web_url_input(raw_url: Any) -> str | None:
    return normalize_web_url(cleanup_public_web_url_candidate(raw_url))


def _strip_markdown_link_target_syntax(
    raw_url: str,
    *,
    outer_closer_budget: dict[str, int] | None = None,
) -> str:
    candidate = str(raw_url or "").strip()
    while candidate and candidate[-1] in _SIMPLE_TRAILING_URL_PUNCTUATION and ")" in candidate:
        candidate = candidate[:-1].rstrip()
    if candidate.endswith(")"):
        candidate = candidate[:-1]
    return cleanup_structured_web_url_target(
        candidate,
        closer_budget=outer_closer_budget,
    )


def _scan_markdown_link_title_end(text: str, *, start: int) -> int | None:
    index = start
    if index >= len(text) or not text[index].isspace():
        return None
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None

    opener = text[index]
    if opener in {"'", '"'}:
        closer = opener
        index += 1
        while index < len(text):
            ch = text[index]
            if ch == "\\":
                index += 2
                continue
            if ch == closer:
                index += 1
                break
            index += 1
        else:
            return None
    elif opener == "(":
        depth = 1
        index += 1
        while index < len(text):
            ch = text[index]
            if ch == "\\":
                index += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            index += 1
        else:
            return None
    else:
        return None

    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and text[index] == ")":
        return index + 1
    return None


def _public_web_url_entry(raw_url: str) -> dict[str, str] | None:
    normalized = normalize_web_url(raw_url)
    if normalized is None:
        return None
    return {
        "url": raw_url,
        "normalized_url": normalized,
        "domain": _domain_for_url(normalized),
    }


def _extract_markdown_autolink_web_urls(text: str) -> list[tuple[dict[str, str], tuple[int, int]]]:
    extracted: list[tuple[dict[str, str], tuple[int, int]]] = []
    cursor = 0
    while True:
        opener = text.find("<", cursor)
        if opener < 0:
            break
        url_start = opener + 1
        if not _looks_like_http_url(text[url_start:]):
            cursor = url_start
            continue
        candidate_end = text.find(">", url_start)
        if candidate_end < 0:
            cursor = url_start
            continue
        raw = cleanup_structured_web_url_target(text[url_start:candidate_end])
        entry = _public_web_url_entry(raw)
        if entry is not None:
            extracted.append((entry, (url_start, candidate_end)))
        cursor = candidate_end + 1
    return extracted


def _extract_markdown_link_web_urls(text: str) -> list[tuple[dict[str, str], tuple[int, int]]]:
    extracted: list[tuple[dict[str, str], tuple[int, int]]] = []
    cursor = 0
    while True:
        close_bracket = text.find("](", cursor)
        if close_bracket < 0:
            break
        label_start = _markdown_link_label_start(text, close_bracket=close_bracket)
        target_start = close_bracket + 2
        while target_start < len(text) and text[target_start].isspace():
            target_start += 1
        if label_start is None or target_start >= len(text):
            cursor = close_bracket + 2
            continue

        if text[target_start] == "<":
            url_start = target_start + 1
            candidate_end = text.find(">", url_start)
            if candidate_end < 0:
                cursor = close_bracket + 2
                continue
            title_end = _scan_markdown_link_title_end(text, start=candidate_end + 1)
            skip_end = title_end or candidate_end + 1
            raw = cleanup_structured_web_url_target(text[url_start:candidate_end])
        else:
            url_start = target_start
            if not _looks_like_http_url(text[url_start:]):
                cursor = close_bracket + 2
                continue
            candidate_end = _scan_public_web_url_candidate_end(text, start=url_start)
            raw_candidate = text[url_start:candidate_end]
            title_end = _scan_markdown_link_title_end(text, start=candidate_end)
            skip_end = title_end or candidate_end
            outer_closer_budget = _leading_url_wrapper_closer_budget(text, start=label_start)
            if title_end is not None:
                raw = cleanup_structured_web_url_target(
                    raw_candidate,
                    closer_budget=outer_closer_budget,
                )
            else:
                raw = _strip_markdown_link_target_syntax(
                    raw_candidate,
                    outer_closer_budget=outer_closer_budget,
                )

        entry = _public_web_url_entry(raw)
        if entry is not None:
            extracted.append((entry, (url_start, skip_end)))
        cursor = max(close_bracket + 2, skip_end)
    return extracted


def _span_overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _expand_markdown_wrapped_url_candidate(
    text: str,
    *,
    start: int,
    end: int,
    closer_budget: dict[str, int] | None = None,
) -> str:
    candidate = text[start:end]
    quote_budget = _leading_url_quote_wrapper_budget(text, start=start)
    marker = _trailing_markdown_url_wrapper_marker(
        candidate,
        closer_budget=closer_budget,
        quote_budget=quote_budget,
    )
    if marker is None:
        return candidate
    expanded_start = start
    while expanded_start > 0 and text[expanded_start - 1] == marker:
        expanded_start -= 1
    if expanded_start == start:
        return candidate
    if expanded_start > 0 and text[expanded_start - 1].isalnum():
        return candidate
    return text[expanded_start:end]


def extract_public_web_urls(text: Any) -> list[dict[str, str]]:
    body = str(text or "")
    candidates: list[tuple[int, dict[str, str]]] = []
    markdown_target_spans: list[tuple[int, int]] = []
    for entry, span in _extract_markdown_autolink_web_urls(body):
        markdown_target_spans.append(span)
        candidates.append((span[0], entry))
    for entry, span in _extract_markdown_link_web_urls(body):
        markdown_target_spans.append(span)
        candidates.append((span[0], entry))

    for match in _URL_RE.finditer(body):
        if _span_overlaps(match.start(), match.end(), markdown_target_spans):
            continue
        closer_budget = _leading_url_wrapper_closer_budget(body, start=match.start())
        quote_budget = _leading_url_quote_wrapper_budget(body, start=match.start())
        raw_candidate = _expand_markdown_wrapped_url_candidate(
            body,
            start=match.start(),
            end=match.end(),
            closer_budget=closer_budget,
        )
        raw = cleanup_public_web_url_candidate(
            raw_candidate,
            closer_budget=closer_budget,
            quote_budget=quote_budget,
        )
        entry = _public_web_url_entry(raw)
        if entry is not None:
            candidates.append((match.start(), entry))

    extracted: list[dict[str, str]] = []
    seen: set[str] = set()
    for _start, entry in sorted(candidates, key=lambda item: item[0]):
        normalized = entry["normalized_url"]
        if normalized in seen:
            continue
        seen.add(normalized)
        extracted.append(entry)
    return extracted


class SessionWebResearchTracker:
    def __init__(self) -> None:
        self._trusted_domains: tuple[str, ...] = ()
        self._user_urls: dict[str, dict[str, Any]] = {}
        self._returned_source_urls: dict[str, dict[str, Any]] = {}
        self._canonical_redirect_urls: dict[str, dict[str, Any]] = {}
        self._search_mediated_urls: dict[str, dict[str, Any]] = {}
        self._provenance_nodes: dict[str, dict[str, Any]] = {}
        self._searches: list[dict[str, Any]] = []
        self._fetches: list[dict[str, Any]] = []
        self._pending_search_indices: list[int] = []
        self._pending_fetch_indices: list[int] = []

    def configure_trusted_domains(self, domains: Iterable[Any] | None) -> tuple[str, ...]:
        """Set the configured trusted-domain allowlist for this tracker.

        Entries are normalized via :func:`normalize_trusted_domain_entry`;
        unusable entries are dropped. This is session configuration, not
        observed evidence: it is never persisted to or hydrated from the
        web-research artifact, and replacing it is idempotent.
        """
        normalized = _dedupe_ordered(
            [normalize_trusted_domain_entry(entry) or "" for entry in list(domains or [])]
        )
        self._trusted_domains = tuple(normalized)
        return self._trusted_domains

    def trusted_domains(self) -> tuple[str, ...]:
        return self._trusted_domains

    def _trusted_domain_for_url(self, normalized_url: str) -> str | None:
        if not self._trusted_domains:
            return None
        host = _domain_for_url(normalized_url)
        if not host:
            return None
        for domain in self._trusted_domains:
            if host == domain or host.endswith("." + domain):
                return domain
        return None

    def classify_fetch_url(self, raw_url: Any) -> str | None:
        classification, _effective_url = self.resolve_fetch_url(raw_url)
        return classification

    def resolve_fetch_url(self, raw_url: Any) -> tuple[str | None, str | None]:
        strict_normalized = normalize_web_url(raw_url)
        if strict_normalized:
            classification = self._classification_for_normalized_url(strict_normalized)
            if classification is not None:
                return classification, strict_normalized
        effective_normalized = canonicalize_web_url_input(raw_url)
        if effective_normalized:
            return self._classification_for_normalized_url(
                effective_normalized
            ), effective_normalized
        return None, strict_normalized

    def fetchable_urls(self, *, limit: int = 10) -> list[str]:
        """Normalized URLs that ``web_fetch`` will currently authorize.

        These are the exact canonical forms ``resolve_fetch_url`` matches against,
        so the model can copy one verbatim into ``web_fetch``. web_search source
        URLs come first (the common "fetch a result" case), then user-provided
        URLs; order within each group is the order they were observed (so "the
        first one" maps to the first source). Bounded so a long session does not
        bloat an error payload.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for url in (*self._returned_source_urls.keys(), *self._user_urls.keys()):
            normalized = str(url or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        if limit is not None and limit > 0:
            return ordered[:limit]
        return ordered

    def observe_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None = None,
    ) -> bool:
        event_name = str(event_type or "").strip()
        if not isinstance(payload, dict):
            return False
        if event_name == "user_message":
            return self._record_user_message(payload=payload, ts=ts, event_id=event_id)
        if event_name == "tool_call":
            name = str(payload.get("name") or "").strip()
            if name == "web_search":
                return self._record_web_search_call(payload=payload, ts=ts, event_id=event_id)
            if name == "web_fetch":
                return self._record_web_fetch_call(payload=payload, ts=ts, event_id=event_id)
            return False
        if event_name == "tool_result":
            name = str(payload.get("name") or "").strip()
            if name == "web_search":
                return self._record_web_search_result(payload=payload, ts=ts, event_id=event_id)
            if name == "web_fetch":
                return self._record_web_fetch_result(payload=payload, ts=ts, event_id=event_id)
            return self._record_trusted_tool_result_urls(
                tool_name=name,
                payload=payload,
                ts=ts,
                event_id=event_id,
            )
        return False

    def artifact_payload(self) -> dict[str, Any]:
        searches = [copy.deepcopy(entry) for entry in self._searches]
        fetches = [copy.deepcopy(entry) for entry in self._fetches]
        user_urls = [copy.deepcopy(entry) for entry in self._user_urls.values()]
        returned_urls = [copy.deepcopy(entry) for entry in self._returned_source_urls.values()]
        redirect_urls = [copy.deepcopy(entry) for entry in self._canonical_redirect_urls.values()]
        search_mediated_urls = [
            copy.deepcopy(entry) for entry in self._search_mediated_urls.values()
        ]
        provenance_nodes = [copy.deepcopy(entry) for entry in self._provenance_nodes.values()]
        deduped_normalized_queries = _dedupe_ordered(
            [str(entry.get("normalized_query") or "") for entry in searches]
            + [
                str(normalized)
                for entry in searches
                for normalized in list(entry.get("normalized_queries") or [])
            ]
        )
        deduped_normalized_fetch_urls = _dedupe_ordered(
            [str(entry.get("normalized_requested_url") or "") for entry in fetches]
        )
        deduped_normalized_final_fetch_urls = _dedupe_ordered(
            [str(entry.get("normalized_final_url") or "") for entry in fetches]
        )
        return {
            "schema_version": 2,
            "user_provided_urls": user_urls,
            "returned_by_web_search_urls": returned_urls,
            "canonical_redirect_urls": redirect_urls,
            "search_mediated_recovery_urls": search_mediated_urls,
            "url_provenance_graph": {
                "node_count": len(provenance_nodes),
                "max_nodes": _MAX_PROVENANCE_NODES,
                "nodes": provenance_nodes,
            },
            "searches": searches,
            "fetches": fetches,
            "deduped_normalized_queries": deduped_normalized_queries,
            "deduped_normalized_user_urls": list(self._user_urls.keys()),
            "deduped_normalized_search_source_urls": list(self._returned_source_urls.keys()),
            "deduped_normalized_canonical_redirect_urls": list(
                self._canonical_redirect_urls.keys()
            ),
            "deduped_normalized_search_mediated_recovery_urls": list(
                self._search_mediated_urls.keys()
            ),
            "deduped_normalized_provenance_graph_urls": list(self._provenance_nodes.keys()),
            "deduped_normalized_fetch_urls": deduped_normalized_fetch_urls,
            "deduped_normalized_final_fetch_urls": deduped_normalized_final_fetch_urls,
        }

    def metrics_payload(self) -> dict[str, int]:
        normalized_queries = [
            str(entry.get("normalized_query") or "")
            for entry in self._searches
            if str(entry.get("normalized_query") or "").strip()
        ]
        normalized_fetches = [
            str(entry.get("normalized_requested_url") or "")
            for entry in self._fetches
            if str(entry.get("normalized_requested_url") or "").strip()
        ]
        query_counts = {
            query: normalized_queries.count(query) for query in _dedupe_ordered(normalized_queries)
        }
        fetch_counts = {
            url: normalized_fetches.count(url) for url in _dedupe_ordered(normalized_fetches)
        }
        return {
            "web_search_calls": len(self._searches),
            "web_fetch_calls": len(self._fetches),
            "unique_web_queries": len(query_counts),
            "unique_web_fetch_urls": len(fetch_counts),
            "duplicate_web_queries": sum(max(count - 1, 0) for count in query_counts.values()),
            "duplicate_web_fetches": sum(max(count - 1, 0) for count in fetch_counts.values()),
            "total_web_sources_returned": sum(
                len(list(entry.get("returned_sources") or [])) for entry in self._searches
            ),
            "total_web_sources_fetched": sum(
                1 for entry in self._fetches if str(entry.get("normalized_final_url") or "").strip()
            ),
        }

    def has_activity(self) -> bool:
        return bool(
            self._user_urls
            or self._returned_source_urls
            or self._canonical_redirect_urls
            or self._search_mediated_urls
            or self._provenance_nodes
            or self._searches
            or self._fetches
        )

    def _classification_for_normalized_url(self, normalized: str) -> str | None:
        for variant in _equivalent_fetch_url_variants(normalized):
            if variant in self._user_urls:
                return _USER_PROVIDED
            if variant in self._returned_source_urls:
                return _RETURNED_BY_WEB_SEARCH
            if variant in self._canonical_redirect_urls:
                return _CANONICAL_REDIRECT
            if variant in self._search_mediated_urls:
                return _SEARCH_MEDIATED_RECOVERY
            node = self._provenance_nodes.get(variant)
            if node is not None:
                classification = str(node.get("provenance_classification") or "").strip()
                if classification in _FETCHABLE_PROVENANCE_CLASSES:
                    return classification
        if self._trusted_domain_for_url(normalized) is not None:
            return _TRUSTED_DOMAIN_ALLOWLIST
        if self._is_same_origin_derived_search_url(normalized):
            return _SAME_ORIGIN_DERIVED
        return None

    def establish_search_mediated_fetch_url(
        self,
        *,
        raw_url: Any,
        query: str,
        source_url: str | None = None,
    ) -> tuple[bool, str | None]:
        normalized = normalize_web_url(raw_url) or canonicalize_web_url_input(raw_url)
        if normalized is None:
            return False, None
        if normalized not in self._search_mediated_urls:
            self._search_mediated_urls[normalized] = {
                "url": str(raw_url or "").strip() or normalized,
                "normalized_url": normalized,
                "domain": _domain_for_url(normalized),
                "query": str(query or "").strip(),
                "source_url": str(source_url or "").strip(),
                "provenance_classification": _SEARCH_MEDIATED_RECOVERY,
            }
            self._add_provenance_node(
                raw_url=raw_url,
                provenance_classification=_SEARCH_MEDIATED_RECOVERY,
                source_event_id=None,
                parent_url=source_url or "",
                parent_source_event_id=None,
                discovery_mechanism="search_mediated_recovery",
                ts=None,
                redirect_chain=(),
                security_validation_status="syntax_validated_fetch_revalidates_target",
            )
            return True, normalized
        return False, normalized

    def _is_same_origin_derived_search_url(self, normalized: str) -> bool:
        return any(
            _same_origin_bounded_derivative(source_url, normalized)
            for source_url in self._returned_source_urls
        )

    def hydrate_from_artifact_payload(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        user_urls = payload.get("user_provided_urls")
        returned_urls = payload.get("returned_by_web_search_urls")
        redirect_urls = payload.get("canonical_redirect_urls")
        search_mediated_urls = payload.get("search_mediated_recovery_urls")
        provenance_graph = payload.get("url_provenance_graph")
        searches = payload.get("searches")
        fetches = payload.get("fetches")
        if not any(
            isinstance(value, list) and value
            for value in (
                user_urls,
                returned_urls,
                redirect_urls,
                search_mediated_urls,
                provenance_graph.get("nodes") if isinstance(provenance_graph, dict) else None,
                searches,
                fetches,
            )
        ):
            return False
        self._user_urls = self._hydrate_url_index(
            raw_entries=user_urls,
            fallback_classification=_USER_PROVIDED,
        )
        self._returned_source_urls = self._hydrate_url_index(
            raw_entries=returned_urls,
            fallback_classification=_RETURNED_BY_WEB_SEARCH,
        )
        self._canonical_redirect_urls = self._hydrate_url_index(
            raw_entries=redirect_urls,
            fallback_classification=_CANONICAL_REDIRECT,
        )
        self._search_mediated_urls = self._hydrate_url_index(
            raw_entries=search_mediated_urls,
            fallback_classification=_SEARCH_MEDIATED_RECOVERY,
        )
        raw_nodes = provenance_graph.get("nodes") if isinstance(provenance_graph, dict) else None
        self._provenance_nodes = self._hydrate_url_index(
            raw_entries=raw_nodes,
            fallback_classification=_TRUSTED_TOOL_OUTPUT,
        )
        self._searches = self._hydrate_event_entries(searches)
        self._fetches = self._hydrate_event_entries(fetches)
        self._pending_search_indices = []
        self._pending_fetch_indices = []
        return self.has_activity()

    def merge_from_artifact_payload(self, payload: dict[str, Any]) -> bool:
        other = SessionWebResearchTracker()
        if not other.hydrate_from_artifact_payload(payload):
            return False
        return self.merge_from_tracker(other)

    def merge_from_tracker(self, other: SessionWebResearchTracker) -> bool:
        changed = False
        changed |= self._merge_url_index(self._user_urls, other._user_urls)
        changed |= self._merge_url_index(self._returned_source_urls, other._returned_source_urls)
        changed |= self._merge_url_index(
            self._canonical_redirect_urls,
            other._canonical_redirect_urls,
        )
        changed |= self._merge_url_index(
            self._search_mediated_urls,
            other._search_mediated_urls,
        )
        changed |= self._merge_url_index(self._provenance_nodes, other._provenance_nodes)
        changed |= self._merge_search_entries(other._searches)
        changed |= self._merge_fetch_entries(other._fetches)
        return changed

    def clear_pending(self) -> None:
        self._pending_search_indices = []
        self._pending_fetch_indices = []

    def _add_provenance_node(
        self,
        *,
        raw_url: Any,
        provenance_classification: str,
        source_event_id: str | None,
        parent_url: str,
        parent_source_event_id: str | None,
        discovery_mechanism: str,
        ts: str | None,
        redirect_chain: tuple[str, ...],
        security_validation_status: str,
        source_type: str = "",
    ) -> bool:
        normalized = normalize_web_url(raw_url) or canonicalize_web_url_input(raw_url)
        if normalized is None:
            return False
        if len(normalized) > _MAX_PROVENANCE_URL_LENGTH:
            return False
        existing = self._provenance_nodes.get(normalized)
        entry = {
            "url": str(raw_url or "").strip() or normalized,
            "normalized_url": normalized,
            "domain": _domain_for_url(normalized),
            "provenance_classification": str(provenance_classification or "").strip(),
            "source_event_id": str(source_event_id or "").strip() or None,
            "parent_url": str(parent_url or "").strip(),
            "parent_source_event_id": str(parent_source_event_id or "").strip() or None,
            "discovery_mechanism": str(discovery_mechanism or "").strip(),
            "source_type": str(source_type or "").strip(),
            "ts": ts,
            "redirect_chain": [
                item
                for item in (
                    normalize_web_url(url) or canonicalize_web_url_input(url) or ""
                    for url in redirect_chain
                )
                if item
            ][:_MAX_URLS_PER_EVENT],
            "security_validation_status": str(security_validation_status or "").strip(),
        }
        if existing is None:
            if len(self._provenance_nodes) >= _MAX_PROVENANCE_NODES:
                return False
            self._provenance_nodes[normalized] = entry
            return True

        changed = False
        for key, value in entry.items():
            if key == "redirect_chain":
                if not existing.get(key) and value:
                    existing[key] = value
                    changed = True
                continue
            current = existing.get(key)
            if (current is None or current == "") and value not in (None, ""):
                existing[key] = value
                changed = True
        return changed

    def _record_urls_from_text(
        self,
        *,
        text: Any,
        provenance_classification: str,
        source_event_id: str | None,
        parent_url: str,
        parent_source_event_id: str | None,
        discovery_mechanism: str,
        source_type: str,
        ts: str | None,
    ) -> bool:
        body = str(text or "")
        if not body:
            return False
        changed = False
        for entry in extract_public_web_urls(body[:_MAX_URL_TEXT_CHARS])[:_MAX_URLS_PER_EVENT]:
            changed |= self._add_provenance_node(
                raw_url=entry["url"],
                provenance_classification=provenance_classification,
                source_event_id=source_event_id,
                parent_url=parent_url,
                parent_source_event_id=parent_source_event_id,
                discovery_mechanism=discovery_mechanism,
                source_type=source_type,
                ts=ts,
                redirect_chain=(),
                security_validation_status="syntax_validated_fetch_revalidates_target",
            )
        return changed

    def _record_trusted_tool_result_urls(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None,
    ) -> bool:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if not result or "error" in result or "error_code" in result:
            return False
        if tool_name in {"fs_read", "fs_read_lines"}:
            return self._record_urls_from_text(
                text=result.get("content"),
                provenance_classification=_TRUSTED_LOCAL_FILE,
                source_event_id=event_id,
                parent_url=str(result.get("path") or ""),
                parent_source_event_id=event_id,
                discovery_mechanism="workspace_file_content",
                source_type=tool_name,
                ts=ts,
            )
        if tool_name in {"web_search", "web_fetch"}:
            return False
        changed = False
        for entry in extract_result_web_urls(result):
            changed |= self._add_provenance_node(
                raw_url=entry["url"],
                provenance_classification=_TRUSTED_TOOL_OUTPUT,
                source_event_id=event_id,
                parent_url=tool_name,
                parent_source_event_id=event_id,
                discovery_mechanism="registered_tool_output",
                source_type=tool_name,
                ts=ts,
                redirect_chain=(),
                security_validation_status="syntax_validated_fetch_revalidates_target",
            )
        return changed

    def _record_user_message(
        self,
        *,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None,
    ) -> bool:
        content = payload.get("content")
        if not isinstance(content, str):
            return False
        changed = False
        for entry in extract_public_web_urls(content):
            normalized = entry["normalized_url"]
            if normalized in self._user_urls:
                continue
            self._user_urls[normalized] = {
                "url": entry["url"],
                "normalized_url": normalized,
                "domain": entry["domain"],
                "ts": ts,
                "step": None,
                "source_event_id": event_id,
                "provenance_classification": _USER_PROVIDED,
            }
            self._add_provenance_node(
                raw_url=entry["url"],
                provenance_classification=_USER_PROVIDED,
                source_event_id=event_id,
                parent_url="",
                parent_source_event_id=None,
                discovery_mechanism="user_message_url",
                ts=ts,
                redirect_chain=(),
                security_validation_status="syntax_validated_fetch_revalidates_target",
            )
            changed = True
        return changed

    def _record_web_search_call(
        self,
        *,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None,
    ) -> bool:
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        query = str(arguments.get("query") or "").strip()
        normalized_query = normalize_web_query(query)
        allowed_domains = [
            str(item or "").strip().lower()
            for item in list(arguments.get("allowed_domains") or [])
            if str(item or "").strip()
        ]
        raw_external = arguments.get("external_web_access")
        external_web_access = raw_external if isinstance(raw_external, bool) else None
        step = payload.get("step") if isinstance(payload.get("step"), int) else None
        entry = {
            "step": step,
            "call_ts": ts,
            "result_ts": None,
            "query": query,
            "normalized_query": normalized_query,
            "queries": [query] if query else [],
            "normalized_queries": [normalized_query] if normalized_query else [],
            "backend": "",
            "backend_adapter": "",
            "protocol": "",
            "chat_protocol": "",
            "search_protocol": "",
            "provider": "",
            "provider_hosted_search": False,
            "external_search_provider": "",
            "citation_count": 0,
            "source_count": 0,
            "allowed_domains": allowed_domains,
            "external_web_access": external_web_access,
            "source_event_id": event_id,
            "response_id": "",
            "sources_truncated": False,
            "returned_sources": [],
            "error": "",
        }
        self._searches.append(entry)
        self._pending_search_indices.append(len(self._searches) - 1)
        return True

    def _record_web_search_result(
        self,
        *,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None,
    ) -> bool:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        step = payload.get("step") if isinstance(payload.get("step"), int) else None
        if self._pending_search_indices:
            entry = self._searches[self._pending_search_indices.pop(0)]
        else:
            entry = {
                "step": step,
                "call_ts": ts,
                "result_ts": None,
                "query": "",
                "normalized_query": "",
                "queries": [],
                "normalized_queries": [],
                "backend": "",
                "backend_adapter": "",
                "protocol": "",
                "chat_protocol": "",
                "search_protocol": "",
                "provider": "",
                "provider_hosted_search": False,
                "external_search_provider": "",
                "citation_count": 0,
                "source_count": 0,
                "allowed_domains": [],
                "external_web_access": None,
                "source_event_id": event_id,
                "response_id": "",
                "sources_truncated": False,
                "returned_sources": [],
                "error": "",
            }
            self._searches.append(entry)
        entry["step"] = entry.get("step") if entry.get("step") is not None else step
        entry["result_ts"] = ts
        if "error" in result:
            entry["error"] = str(result.get("error") or "").strip()
            return True

        query = str(result.get("query") or entry.get("query") or "").strip()
        normalized_query = normalize_web_query(query)
        raw_queries = result.get("queries")
        query_list = (
            [str(item or "").strip() for item in raw_queries]
            if isinstance(raw_queries, list)
            else []
        )
        query_list = [item for item in query_list if item]
        if query and query not in query_list:
            query_list.insert(0, query)
        normalized_queries = _dedupe_ordered([normalize_web_query(item) for item in query_list])
        returned_sources: list[dict[str, Any]] = []
        for raw_source in list(result.get("sources") or []):
            if not isinstance(raw_source, dict):
                continue
            raw_url = str(raw_source.get("url") or "").strip()
            normalized_url = normalize_web_url(raw_url)
            if normalized_url is None:
                continue
            source_entry = {
                "title": str(raw_source.get("title") or "").strip(),
                "url": raw_url,
                "normalized_url": normalized_url,
                "domain": _domain_for_url(normalized_url),
                "snippet": str(raw_source.get("snippet") or "").strip(),
                "provenance_classification": _RETURNED_BY_WEB_SEARCH,
            }
            returned_sources.append(source_entry)
            if normalized_url not in self._returned_source_urls:
                self._returned_source_urls[normalized_url] = {
                    "title": source_entry["title"],
                    "url": raw_url,
                    "normalized_url": normalized_url,
                    "domain": source_entry["domain"],
                    "snippet": source_entry["snippet"],
                    "ts": ts,
                    "step": entry.get("step"),
                    "backend": str(result.get("backend") or "").strip(),
                    "source_event_id": event_id,
                    "provenance_classification": _RETURNED_BY_WEB_SEARCH,
                }
                self._add_provenance_node(
                    raw_url=raw_url,
                    provenance_classification=_RETURNED_BY_WEB_SEARCH,
                    source_event_id=event_id,
                    parent_url="",
                    parent_source_event_id=None,
                    discovery_mechanism="web_search_source",
                    ts=ts,
                    redirect_chain=(),
                    security_validation_status="syntax_validated_fetch_revalidates_target",
                )

        entry["query"] = query
        entry["normalized_query"] = normalized_query
        entry["queries"] = query_list
        entry["normalized_queries"] = normalized_queries
        entry["backend"] = str(result.get("backend") or "").strip()
        entry["backend_adapter"] = str(
            result.get("backend_adapter") or result.get("backend") or ""
        ).strip()
        entry["protocol"] = str(result.get("protocol") or entry["backend_adapter"]).strip()
        entry["chat_protocol"] = str(result.get("chat_protocol") or entry["protocol"]).strip()
        entry["search_protocol"] = str(
            result.get("search_protocol") or entry["backend_adapter"]
        ).strip()
        entry["provider"] = str(result.get("backend") or "").strip()
        entry["provider_hosted_search"] = bool(result.get("provider_hosted_search"))
        entry["external_search_provider"] = str(
            result.get("external_search_provider") or ""
        ).strip()
        raw_citation_count = result.get("citation_count")
        if isinstance(raw_citation_count, int):
            entry["citation_count"] = raw_citation_count
        else:
            citations = result.get("citations")
            entry["citation_count"] = len(citations) if isinstance(citations, list) else 0
        raw_source_count = result.get("source_count")
        if isinstance(raw_source_count, int):
            entry["source_count"] = raw_source_count
        else:
            entry["source_count"] = len(returned_sources)
        entry["allowed_domains"] = [
            str(item or "").strip().lower()
            for item in list(result.get("allowed_domains") or entry.get("allowed_domains") or [])
            if str(item or "").strip()
        ]
        raw_external = result.get("external_web_access")
        if isinstance(raw_external, bool):
            entry["external_web_access"] = raw_external
        entry["response_id"] = str(result.get("response_id") or "").strip()
        entry["sources_truncated"] = bool(result.get("sources_truncated"))
        entry["returned_sources"] = returned_sources
        entry["error"] = ""
        return True

    def _record_web_fetch_call(
        self,
        *,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None,
    ) -> bool:
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        raw_requested_url = str(arguments.get("url") or "").strip()
        provenance_classification, effective_requested_url = self.resolve_fetch_url(
            raw_requested_url
        )
        requested_url = effective_requested_url or raw_requested_url
        normalized_requested_url = normalize_web_url(requested_url)
        step = payload.get("step") if isinstance(payload.get("step"), int) else None
        entry = {
            "step": step,
            "call_ts": ts,
            "result_ts": None,
            "requested_url": requested_url,
            "normalized_requested_url": normalized_requested_url,
            "raw_input_url": (
                raw_requested_url
                if raw_requested_url and raw_requested_url != requested_url
                else ""
            ),
            "final_url": "",
            "normalized_final_url": "",
            "status_code": None,
            "content_type": "",
            "title": "",
            "backend": "",
            "provenance_classification": provenance_classification or "",
            "source_event_id": event_id,
            "error": "",
            "error_code": "",
        }
        self._fetches.append(entry)
        self._pending_fetch_indices.append(len(self._fetches) - 1)
        return True

    def _record_web_fetch_result(
        self,
        *,
        payload: dict[str, Any],
        ts: str | None,
        event_id: str | None,
    ) -> bool:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        step = payload.get("step") if isinstance(payload.get("step"), int) else None
        if self._pending_fetch_indices:
            entry = self._fetches[self._pending_fetch_indices.pop(0)]
        else:
            entry = {
                "step": step,
                "call_ts": ts,
                "result_ts": None,
                "requested_url": "",
                "normalized_requested_url": None,
                "raw_input_url": "",
                "final_url": "",
                "normalized_final_url": "",
                "status_code": None,
                "content_type": "",
                "title": "",
                "backend": "",
                "provenance_classification": "",
                "source_event_id": event_id,
                "error": "",
                "error_code": "",
            }
            self._fetches.append(entry)
        entry["step"] = entry.get("step") if entry.get("step") is not None else step
        entry["result_ts"] = ts

        requested_url = str(result.get("url") or entry.get("requested_url") or "").strip()
        raw_input_url = str(result.get("raw_input_url") or entry.get("raw_input_url") or "").strip()
        normalized_requested_url = normalize_web_url(requested_url)
        if requested_url:
            entry["requested_url"] = requested_url
        if normalized_requested_url:
            entry["normalized_requested_url"] = normalized_requested_url
        if raw_input_url and raw_input_url != requested_url:
            entry["raw_input_url"] = raw_input_url
        if not entry.get("provenance_classification"):
            entry["provenance_classification"] = (
                self.classify_fetch_url(raw_input_url or requested_url) or ""
            )

        if "error" in result:
            entry["error"] = str(result.get("error") or "").strip()
            entry["error_code"] = str(result.get("error_code") or "").strip()
            return True

        final_url = str(result.get("final_url") or "").strip()
        normalized_final_url = normalize_web_url(final_url)
        entry["final_url"] = final_url
        entry["normalized_final_url"] = normalized_final_url or ""
        entry["status_code"] = (
            result.get("status_code") if result.get("status_code") is not None else None
        )
        entry["content_type"] = str(result.get("content_type") or "").strip()
        entry["title"] = str(result.get("title") or "").strip()
        entry["backend"] = str(result.get("backend") or "").strip()
        entry["error"] = ""
        entry["error_code"] = ""
        provenance = str(entry.get("provenance_classification") or "").strip()
        if (
            normalized_final_url
            and normalized_requested_url
            and normalized_final_url != normalized_requested_url
            and provenance
            in {
                _USER_PROVIDED,
                _RETURNED_BY_WEB_SEARCH,
                _SEARCH_MEDIATED_RECOVERY,
                _SAME_ORIGIN_DERIVED,
            }
            and normalized_final_url not in self._canonical_redirect_urls
        ):
            self._canonical_redirect_urls[normalized_final_url] = {
                "url": final_url,
                "normalized_url": normalized_final_url,
                "domain": _domain_for_url(normalized_final_url),
                "source_url": requested_url,
                "source_provenance_classification": provenance,
                "source_event_id": event_id,
                "provenance_classification": _CANONICAL_REDIRECT,
            }
            self._add_provenance_node(
                raw_url=final_url,
                provenance_classification=_CANONICAL_REDIRECT,
                source_event_id=event_id,
                parent_url=requested_url,
                parent_source_event_id=str(entry.get("source_event_id") or "").strip() or None,
                discovery_mechanism="validated_redirect",
                ts=ts,
                redirect_chain=(requested_url, final_url),
                security_validation_status="validated_by_web_fetch",
            )
        self._record_urls_from_text(
            text=result.get("content"),
            provenance_classification=_FETCHED_PAGE_LINK,
            source_event_id=event_id,
            parent_url=final_url or requested_url,
            parent_source_event_id=event_id,
            discovery_mechanism="fetched_page_content",
            source_type="web_fetch_result",
            ts=ts,
        )
        return True

    def _hydrate_url_index(
        self,
        *,
        raw_entries: Any,
        fallback_classification: str,
    ) -> dict[str, dict[str, Any]]:
        hydrated: dict[str, dict[str, Any]] = {}
        if not isinstance(raw_entries, list):
            return hydrated
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            normalized_url = normalize_web_url(
                raw_entry.get("normalized_url") or raw_entry.get("url")
            )
            if not normalized_url or normalized_url in hydrated:
                continue
            hydrated[normalized_url] = {
                "url": str(raw_entry.get("url") or "").strip() or normalized_url,
                "normalized_url": normalized_url,
                "domain": str(raw_entry.get("domain") or _domain_for_url(normalized_url)).strip(),
                "ts": str(raw_entry.get("ts") or "").strip() or None,
                "step": raw_entry.get("step") if isinstance(raw_entry.get("step"), int) else None,
                "source_event_id": str(raw_entry.get("source_event_id") or "").strip() or None,
                "parent_url": str(raw_entry.get("parent_url") or "").strip(),
                "parent_source_event_id": (
                    str(raw_entry.get("parent_source_event_id") or "").strip() or None
                ),
                "discovery_mechanism": str(raw_entry.get("discovery_mechanism") or "").strip(),
                "source_type": str(raw_entry.get("source_type") or "").strip(),
                "redirect_chain": [
                    str(item or "").strip()
                    for item in list(raw_entry.get("redirect_chain") or [])
                    if str(item or "").strip()
                ][:_MAX_URLS_PER_EVENT],
                "security_validation_status": str(
                    raw_entry.get("security_validation_status") or ""
                ).strip(),
                "provenance_classification": str(
                    raw_entry.get("provenance_classification") or fallback_classification
                ).strip()
                or fallback_classification,
                "title": str(raw_entry.get("title") or "").strip(),
                "snippet": str(raw_entry.get("snippet") or "").strip(),
                "backend": str(raw_entry.get("backend") or "").strip(),
            }
        return hydrated

    def _hydrate_event_entries(self, raw_entries: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_entries, list):
            return []
        hydrated: list[dict[str, Any]] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            hydrated.append(copy.deepcopy(raw_entry))
        return hydrated

    def _merge_url_index(
        self,
        existing: dict[str, dict[str, Any]],
        incoming: dict[str, dict[str, Any]],
    ) -> bool:
        changed = False
        for normalized_url, incoming_entry in incoming.items():
            current = existing.get(normalized_url)
            if current is None:
                existing[normalized_url] = copy.deepcopy(incoming_entry)
                changed = True
                continue
            merged = self._merge_url_index_entry(current, incoming_entry)
            if merged != current:
                existing[normalized_url] = merged
                changed = True
        return changed

    def _merge_url_index_entry(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        merged = copy.deepcopy(existing)
        for key in (
            "url",
            "domain",
            "ts",
            "title",
            "snippet",
            "backend",
            "provenance_classification",
            "source_event_id",
            "parent_url",
            "parent_source_event_id",
            "discovery_mechanism",
            "source_type",
            "security_validation_status",
        ):
            current_value = str(merged.get(key) or "").strip()
            incoming_value = str(incoming.get(key) or "").strip()
            if not current_value and incoming_value:
                merged[key] = incoming_value
        if not list(merged.get("redirect_chain") or []) and list(
            incoming.get("redirect_chain") or []
        ):
            merged["redirect_chain"] = list(incoming.get("redirect_chain") or [])[
                :_MAX_URLS_PER_EVENT
            ]
        if not str(merged.get("normalized_url") or "").strip():
            merged["normalized_url"] = str(incoming.get("normalized_url") or "").strip()
        if not isinstance(merged.get("step"), int) and isinstance(incoming.get("step"), int):
            merged["step"] = incoming["step"]
        return merged

    def _merge_search_entries(self, incoming_entries: list[dict[str, Any]]) -> bool:
        changed = False
        for incoming_entry in incoming_entries:
            incoming = copy.deepcopy(incoming_entry)
            match_index = self._find_matching_search_index(incoming)
            if match_index is None:
                self._searches.append(incoming)
                changed = True
                continue
            merged = self._merge_search_entry(self._searches[match_index], incoming)
            if merged != self._searches[match_index]:
                self._searches[match_index] = merged
                changed = True
        return changed

    def _merge_fetch_entries(self, incoming_entries: list[dict[str, Any]]) -> bool:
        changed = False
        for incoming_entry in incoming_entries:
            incoming = copy.deepcopy(incoming_entry)
            match_index = self._find_matching_fetch_index(incoming)
            if match_index is None:
                self._fetches.append(incoming)
                changed = True
                continue
            merged = self._merge_fetch_entry(self._fetches[match_index], incoming)
            if merged != self._fetches[match_index]:
                self._fetches[match_index] = merged
                changed = True
        return changed

    def _find_matching_search_index(self, incoming: dict[str, Any]) -> int | None:
        incoming_fingerprint = _canonical_json(incoming)
        incoming_keys = self._search_merge_keys(incoming)
        for index, existing in enumerate(self._searches):
            if _canonical_json(existing) == incoming_fingerprint:
                return index
        if not incoming_keys:
            return None
        for index, existing in enumerate(self._searches):
            if incoming_keys & self._search_merge_keys(existing):
                return index
        return None

    def _find_matching_fetch_index(self, incoming: dict[str, Any]) -> int | None:
        incoming_fingerprint = _canonical_json(incoming)
        incoming_keys = self._fetch_merge_keys(incoming)
        for index, existing in enumerate(self._fetches):
            if _canonical_json(existing) == incoming_fingerprint:
                return index
        if not incoming_keys:
            return None
        for index, existing in enumerate(self._fetches):
            if incoming_keys & self._fetch_merge_keys(existing):
                return index
        return None

    def _search_merge_keys(self, entry: dict[str, Any]) -> set[tuple[Any, ...]]:
        keys: set[tuple[Any, ...]] = set()
        step = entry.get("step") if isinstance(entry.get("step"), int) else None
        call_ts = str(entry.get("call_ts") or "").strip()
        result_ts = str(entry.get("result_ts") or "").strip()
        normalized_query = str(entry.get("normalized_query") or "").strip()
        normalized_queries = tuple(
            _dedupe_ordered(
                [str(item or "").strip() for item in list(entry.get("normalized_queries") or [])]
            )
        )
        allowed_domains = tuple(
            _dedupe_ordered(
                [
                    str(item or "").strip().lower()
                    for item in list(entry.get("allowed_domains") or [])
                ]
            )
        )
        if step is not None and call_ts and normalized_query:
            keys.add(("search", "step_call_query", step, call_ts, normalized_query))
        if step is not None and result_ts and normalized_query:
            keys.add(("search", "step_result_query", step, result_ts, normalized_query))
        if call_ts and normalized_query:
            keys.add(("search", "call_query", call_ts, normalized_query))
        if result_ts and normalized_query:
            keys.add(("search", "result_query", result_ts, normalized_query))
        if step is not None and normalized_query and allowed_domains:
            keys.add(("search", "step_query_domains", step, normalized_query, allowed_domains))
        if step is not None and normalized_query:
            keys.add(("search", "step_query", step, normalized_query))
        if step is not None and normalized_queries:
            keys.add(("search", "step_queries", step, normalized_queries))
        return keys

    def _fetch_merge_keys(self, entry: dict[str, Any]) -> set[tuple[Any, ...]]:
        keys: set[tuple[Any, ...]] = set()
        step = entry.get("step") if isinstance(entry.get("step"), int) else None
        call_ts = str(entry.get("call_ts") or "").strip()
        result_ts = str(entry.get("result_ts") or "").strip()
        normalized_requested_url = str(entry.get("normalized_requested_url") or "").strip()
        normalized_final_url = str(entry.get("normalized_final_url") or "").strip()
        if step is not None and call_ts and normalized_requested_url:
            keys.add(("fetch", "step_call_requested", step, call_ts, normalized_requested_url))
        if step is not None and result_ts and normalized_requested_url:
            keys.add(("fetch", "step_result_requested", step, result_ts, normalized_requested_url))
        if call_ts and normalized_requested_url:
            keys.add(("fetch", "call_requested", call_ts, normalized_requested_url))
        if result_ts and normalized_requested_url:
            keys.add(("fetch", "result_requested", result_ts, normalized_requested_url))
        if step is not None and normalized_requested_url:
            keys.add(("fetch", "step_requested", step, normalized_requested_url))
        if step is not None and normalized_final_url:
            keys.add(("fetch", "step_final", step, normalized_final_url))
        return keys

    def _merge_search_entry(
        self, existing: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(existing)
        if not isinstance(merged.get("step"), int) and isinstance(incoming.get("step"), int):
            merged["step"] = incoming["step"]
        for key in (
            "call_ts",
            "result_ts",
            "query",
            "normalized_query",
            "backend",
            "provider",
            "response_id",
            "error",
        ):
            current_value = str(merged.get(key) or "").strip()
            incoming_value = str(incoming.get(key) or "").strip()
            if not current_value and incoming_value:
                merged[key] = incoming_value
        if merged.get("external_web_access") is None and isinstance(
            incoming.get("external_web_access"), bool
        ):
            merged["external_web_access"] = incoming["external_web_access"]
        merged["sources_truncated"] = bool(
            merged.get("sources_truncated") or incoming.get("sources_truncated")
        )
        merged["queries"] = _dedupe_ordered(
            [str(item or "").strip() for item in list(merged.get("queries") or [])]
            + [str(item or "").strip() for item in list(incoming.get("queries") or [])]
        )
        merged["normalized_queries"] = _dedupe_ordered(
            [str(item or "").strip() for item in list(merged.get("normalized_queries") or [])]
            + [str(item or "").strip() for item in list(incoming.get("normalized_queries") or [])]
        )
        merged["allowed_domains"] = _dedupe_ordered(
            [str(item or "").strip().lower() for item in list(merged.get("allowed_domains") or [])]
            + [
                str(item or "").strip().lower()
                for item in list(incoming.get("allowed_domains") or [])
            ]
        )
        merged["returned_sources"] = self._merge_source_entries(
            list(merged.get("returned_sources") or []),
            list(incoming.get("returned_sources") or []),
        )
        return merged

    def _merge_fetch_entry(
        self, existing: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(existing)
        if not isinstance(merged.get("step"), int) and isinstance(incoming.get("step"), int):
            merged["step"] = incoming["step"]
        for key in (
            "call_ts",
            "result_ts",
            "requested_url",
            "normalized_requested_url",
            "raw_input_url",
            "final_url",
            "normalized_final_url",
            "content_type",
            "title",
            "backend",
            "provenance_classification",
            "error",
            "error_code",
        ):
            current_value = str(merged.get(key) or "").strip()
            incoming_value = str(incoming.get(key) or "").strip()
            if not current_value and incoming_value:
                merged[key] = incoming_value
        if merged.get("status_code") is None and incoming.get("status_code") is not None:
            merged["status_code"] = incoming.get("status_code")
        return merged

    def _merge_source_entries(
        self,
        existing_entries: list[dict[str, Any]],
        incoming_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged_entries = [
            copy.deepcopy(entry) for entry in existing_entries if isinstance(entry, dict)
        ]
        index_by_url: dict[str, int] = {}
        for index, entry in enumerate(merged_entries):
            normalized_url = normalize_web_url(entry.get("normalized_url") or entry.get("url"))
            if normalized_url:
                entry["normalized_url"] = normalized_url
                index_by_url[normalized_url] = index
        for incoming_entry in incoming_entries:
            if not isinstance(incoming_entry, dict):
                continue
            normalized_url = normalize_web_url(
                incoming_entry.get("normalized_url") or incoming_entry.get("url")
            )
            if not normalized_url:
                continue
            incoming_copy = copy.deepcopy(incoming_entry)
            incoming_copy["normalized_url"] = normalized_url
            existing_index = index_by_url.get(normalized_url)
            if existing_index is None:
                merged_entries.append(incoming_copy)
                index_by_url[normalized_url] = len(merged_entries) - 1
                continue
            current = merged_entries[existing_index]
            for key in ("title", "url", "domain", "snippet", "provenance_classification"):
                current_value = str(current.get(key) or "").strip()
                incoming_value = str(incoming_copy.get(key) or "").strip()
                if not current_value and incoming_value:
                    current[key] = incoming_value
        return merged_entries


def build_web_research_artifact_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    tracker = SessionWebResearchTracker()
    for index, event in enumerate(events, start=1):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        tracker.observe_event(
            event_type=str(event.get("type") or "").strip(),
            payload=payload,
            ts=str(event.get("ts") or "").strip() or None,
            event_id=str(event.get("event_id") or "").strip() or f"event:{index}",
        )
    return tracker.artifact_payload()


def build_web_research_metrics_from_events(events: list[dict[str, Any]]) -> dict[str, int]:
    tracker = SessionWebResearchTracker()
    for index, event in enumerate(events, start=1):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        tracker.observe_event(
            event_type=str(event.get("type") or "").strip(),
            payload=payload,
            ts=str(event.get("ts") or "").strip() or None,
            event_id=str(event.get("event_id") or "").strip() or f"event:{index}",
        )
    return tracker.metrics_payload()


def build_web_research_metrics_from_artifact_payload(payload: dict[str, Any]) -> dict[str, int]:
    tracker = SessionWebResearchTracker()
    if not tracker.hydrate_from_artifact_payload(payload):
        return SessionWebResearchTracker().metrics_payload()
    return tracker.metrics_payload()


def web_research_artifact_has_activity(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in (
        "user_provided_urls",
        "returned_by_web_search_urls",
        "searches",
        "fetches",
    ):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return True
    graph = payload.get("url_provenance_graph")
    if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) and graph["nodes"]:
        return True
    return False
