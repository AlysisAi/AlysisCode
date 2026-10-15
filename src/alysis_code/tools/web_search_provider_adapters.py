from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..llm.provider_limits import (
    ProviderRetrySettings,
    best_effort_provider_key,
    mark_provider_call_non_retryable,
    run_provider_limited_call,
)
from ..safety import SafeHttpError, safe_http_request
from ..safety.safe_http import Resolver
from ..web_research import extract_public_web_urls, normalize_web_url
from .http_timeout import build_http_timeout_budget, format_http_timeout_error


class ProviderWebSearchError(RuntimeError):
    pass


_MAX_SNIPPET_CHARS = 500
_PERPLEXITY_API_HOST = "api.perplexity.ai"
_PERPLEXITY_INTEGRATION = "alysis-code"


def _truncate_snippet(raw_value: Any) -> str | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    return text[: _MAX_SNIPPET_CHARS - 3].rstrip() + "..."


def _extract_error_message(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        message = str(error_obj.get("message") or error_obj.get("detail") or "").strip()
        if message:
            code = str(error_obj.get("code") or "").strip()
            return f"{code}: {message}" if code else message
    for key in ("message", "detail", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _response_error(provider_label: str, response: httpx.Response) -> ProviderWebSearchError:
    try:
        data = response.json()
    except Exception:
        body = response.text
        if len(body) > 1000:
            body = body[:1000] + "...(truncated)"
        return ProviderWebSearchError(f"{provider_label} error {response.status_code}: {body}")
    message = _extract_error_message(data)
    if message:
        return ProviderWebSearchError(f"{provider_label} error {response.status_code}: {message}")
    return ProviderWebSearchError(f"{provider_label} error {response.status_code}: {data!r}")


def _post_json(
    *,
    provider_label: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float,
    transport: httpx.BaseTransport | None,
    resolver: Resolver | None,
    provider_key: str,
    provider_concurrency_caps: dict[str, int] | None,
    provider_retry_settings: ProviderRetrySettings | None,
    operation: str,
) -> dict[str, Any]:
    timeout_budget = build_http_timeout_budget(timeout_s, profile="search")

    def _send_request() -> httpx.Response:
        try:
            response = asyncio.run(
                safe_http_request(
                    "POST",
                    url,
                    timeout=timeout_s,
                    headers=headers,
                    json=payload,
                    _transport=transport,  # type: ignore[arg-type]
                    _resolver=resolver,
                )
            )
        except httpx.TimeoutException as e:
            error = ProviderWebSearchError(
                format_http_timeout_error(
                    operation=f"{provider_label} web_search",
                    budget=timeout_budget,
                    error=e,
                )
            )
            # A read timeout means the provider accepted the request but did not
            # answer within the budget; retrying immediately with the same budget
            # just times out again and doubles the dead air. Fail fast on read
            # timeouts while leaving connect/transient timeouts retryable.
            if isinstance(e, httpx.ReadTimeout):
                mark_provider_call_non_retryable(error)
            raise error from e
        except SafeHttpError as e:
            raise ProviderWebSearchError(f"{provider_label} request blocked: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise ProviderWebSearchError(f"{provider_label} request failed: {e}") from e
        if response.status_code >= 400:
            raise _response_error(provider_label, response)
        return response

    response = run_provider_limited_call(
        call=_send_request,
        provider_key=provider_key,
        provider_concurrency_caps=provider_concurrency_caps,
        retry_settings=provider_retry_settings,
        operation=operation,
    )
    try:
        data = response.json()
    except Exception as e:  # noqa: BLE001
        raise ProviderWebSearchError(f"{provider_label} returned non-JSON response") from e
    if not isinstance(data, dict):
        raise ProviderWebSearchError(f"Unexpected {provider_label} payload: expected JSON object")
    return data


def _host_matches_allowed_domains(url: str, allowed_domains: list[str] | None) -> bool:
    if not allowed_domains:
        return True
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    for domain in allowed_domains:
        normalized = str(domain or "").strip().rstrip(".").lower()
        if host == normalized or host.endswith(f".{normalized}"):
            return True
    return False


def _normalize_source(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    nested = raw.get("url_citation")
    if isinstance(nested, dict):
        raw = nested
    web = raw.get("web")
    if isinstance(web, dict):
        raw = web
    url = str(
        raw.get("url") or raw.get("uri") or raw.get("link") or raw.get("origin_url") or ""
    ).strip()
    if not url:
        return None
    normalized_url = normalize_web_url(url) or url
    source: dict[str, Any] = {
        "url": normalized_url,
        "title": str(raw.get("title") or raw.get("name") or raw.get("source") or "").strip(),
    }
    snippet = _truncate_snippet(
        raw.get("snippet")
        or raw.get("content")
        or raw.get("summary")
        or raw.get("text")
        or raw.get("cited_text")
    )
    if snippet:
        source["snippet"] = snippet
    return source


def _dedupe_sources(
    sources: list[dict[str, Any]],
    *,
    allowed_domains: list[str] | None = None,
    max_sources: int,
) -> tuple[list[dict[str, Any]], bool]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        normalized = _normalize_source(source)
        if normalized is None:
            continue
        url = str(normalized.get("url") or "").strip()
        if not url or url in seen:
            continue
        if not _host_matches_allowed_domains(url, allowed_domains):
            continue
        seen.add(url)
        deduped.append(normalized)
    return deduped[:max_sources], len(deduped) > max_sources


def _citations_from_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        citations.append(
            {
                "title": str(source.get("title") or "").strip(),
                "url": url,
                "start_index": source.get("start_index"),
                "end_index": source.get("end_index"),
            }
        )
    return citations


def _finalize_citations(
    citations: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    source_urls = {str(source.get("url") or "").strip() for source in sources}
    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for citation in citations:
        url = (
            normalize_web_url(str(citation.get("url") or "").strip())
            or str(citation.get("url") or "").strip()
        )
        if not url or url not in source_urls or url in seen:
            continue
        seen.add(url)
        filtered.append(
            {
                "title": str(citation.get("title") or "").strip(),
                "url": url,
                "start_index": citation.get("start_index"),
                "end_index": citation.get("end_index"),
            }
        )
    if filtered:
        return filtered[:max_results]
    return _citations_from_sources(sources)[:max_results]


def _sources_from_answer_urls(answer: str) -> list[dict[str, Any]]:
    return [
        {"url": entry["normalized_url"], "title": ""} for entry in extract_public_web_urls(answer)
    ]


def _collect_source_dicts(value: Any) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("url") or node.get("uri") or node.get("link") or node.get("origin_url"):
                sources.append(node)
            for nested_key in (
                "choices",
                "message",
                "sources",
                "citations",
                "annotations",
                "url_citation",
                "search_results",
                "search_result",
                "results",
                "web_search",
                "action",
                "data",
                "items",
                "output",
                "content",
            ):
                nested = node.get(nested_key)
                if isinstance(nested, (list, dict)):
                    _walk(nested)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(value)
    return sources


def _collect_queries(value: Any) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    def _append(raw: Any) -> None:
        query_text = str(raw or "").strip()
        if query_text and query_text not in seen:
            seen.add(query_text)
            queries.append(query_text)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("query", "search_query", "keywords"):
                if key in node:
                    _append(node.get(key))
            for nested_key in (
                "choices",
                "message",
                "queries",
                "search_intent",
                "search_results",
                "search_result",
                "web_search",
                "action",
                "tool_calls",
                "arguments",
                "input",
                "output",
                "content",
            ):
                nested = node.get(nested_key)
                if isinstance(nested, (list, dict)):
                    _walk(nested)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    _append(item)
                else:
                    _walk(item)

    _walk(value)
    return queries


def _responses_answer(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = data.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if not isinstance(chunk, dict):
                    continue
                text = chunk.get("text") or chunk.get("content")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()


def _domain_prompt_suffix(allowed_domains: list[str] | None) -> str:
    if not allowed_domains:
        return ""
    return "\nOnly use sources from these domains: " + ", ".join(allowed_domains) + "."


def _chat_completion_answer(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            if parts:
                return "".join(parts).strip()
    return ""


def _chat_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list):
        return {}
    for choice in choices:
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            return choice["message"]
    return {}


def _extract_chat_annotations(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    message = _chat_message(data)
    annotations = message.get("annotations")
    sources: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    if not isinstance(annotations, list):
        return sources, citations
    for annotation in annotations:
        source = _normalize_source(annotation)
        if source is None:
            continue
        url_citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
        citation_payload = url_citation if isinstance(url_citation, dict) else annotation
        source["start_index"] = citation_payload.get("start_index")
        source["end_index"] = citation_payload.get("end_index")
        sources.append(source)
        citations.append(
            {
                "title": str(source.get("title") or "").strip(),
                "url": str(source.get("url") or "").strip(),
                "start_index": source.get("start_index"),
                "end_index": source.get("end_index"),
            }
        )
    return sources, citations


def _minimax_coding_plan_search_url(base_url: str) -> str:
    normalized = normalize_provider_base_url(base_url)
    if normalized.endswith("/v1"):
        return f"{normalized}/coding_plan/search"
    return f"{normalized}/v1/coding_plan/search"


def minimax_coding_plan_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    data = _post_json(
        provider_label="MiniMax Token Plan",
        url=_minimax_coding_plan_search_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "MM-API-Source": "Minimax-MCP",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload={"q": query},
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="minimax",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="minimax_coding_plan_search",
    )
    base_response = data.get("base_resp")
    if isinstance(base_response, dict):
        status_code = base_response.get("status_code")
        if status_code not in (None, 0, "0"):
            status_message = str(base_response.get("status_msg") or "search failed").strip()
            raise ProviderWebSearchError(
                f"MiniMax Token Plan error {status_code}: {status_message}"
            )

    organic = data.get("organic")
    if not isinstance(organic, list) and isinstance(data.get("data"), dict):
        organic = data["data"].get("organic")
    raw_sources = [item for item in organic or [] if isinstance(item, dict)]
    sources, sources_truncated = _dedupe_sources(
        raw_sources,
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("MiniMax Token Plan web_search did not return sources")

    related = data.get("related_searches")
    if not isinstance(related, list) and isinstance(data.get("data"), dict):
        related = data["data"].get("related_searches")
    queries = [query]
    for item in related or []:
        related_query = (
            str(item.get("query") or "").strip()
            if isinstance(item, dict)
            else str(item or "").strip()
        )
        if related_query and related_query not in queries:
            queries.append(related_query)

    return {
        "query": query,
        "answer": str(data.get("answer") or "").strip(),
        "citations": _citations_from_sources(sources),
        "sources": sources,
        "queries": queries,
        "model": model,
        "backend": "minimax_coding_plan",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or data.get("request_id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def _cohere_v1_chat_url(base_url: str) -> str:
    split = urlsplit(str(base_url or "").strip())
    host = (split.hostname or "").rstrip(".").lower()
    if host in {"api.cohere.ai", "api.cohere.com"}:
        return urlunsplit(("https", "api.cohere.com", "/v1/chat", "", ""))
    normalized = normalize_provider_base_url(base_url)
    if normalized.endswith("/compatibility/v1"):
        normalized = normalized.removesuffix("/compatibility/v1")
    elif normalized.endswith("/v1"):
        normalized = normalized.removesuffix("/v1")
    return f"{normalized}/v1/chat"


def cohere_web_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    data = _post_json(
        provider_label="Cohere",
        url=_cohere_v1_chat_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload={
            "model": model,
            "message": f"{query}{_domain_prompt_suffix(allowed_domains)}",
            "connectors": [{"id": "web-search"}],
            "prompt_truncation": "AUTO",
        },
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="cohere",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="cohere_web_search",
    )

    documents = data.get("documents")
    raw_documents = [item for item in documents or [] if isinstance(item, dict)]
    source_by_document_id: dict[str, dict[str, Any]] = {}
    for document in raw_documents:
        source = _normalize_source(document)
        document_id = str(document.get("id") or "").strip()
        if source is not None and document_id:
            source_by_document_id[document_id] = source
    sources, sources_truncated = _dedupe_sources(
        raw_documents,
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Cohere web-search connector did not return sources")

    citations: list[dict[str, Any]] = []
    raw_citations = data.get("citations")
    for citation in raw_citations or []:
        if not isinstance(citation, dict):
            continue
        document_ids = citation.get("document_ids")
        for document_id in document_ids if isinstance(document_ids, list) else []:
            source = source_by_document_id.get(str(document_id or "").strip())
            if source is None:
                continue
            citations.append(
                {
                    "title": str(source.get("title") or "").strip(),
                    "url": str(source.get("url") or "").strip(),
                    "start_index": citation.get("start"),
                    "end_index": citation.get("end"),
                }
            )
    citations = _finalize_citations(citations, sources, max_results=max_results)

    queries: list[str] = []
    raw_queries = data.get("search_queries")
    for item in raw_queries or []:
        raw_query = item.get("text") if isinstance(item, dict) else item
        query_text = str(raw_query or "").strip()
        if query_text and query_text not in queries:
            queries.append(query_text)

    return {
        "query": query,
        "answer": str(data.get("text") or "").strip(),
        "citations": citations,
        "sources": sources,
        "queries": queries or [query],
        "model": model,
        "backend": "cohere_web_search",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": (
            str(data.get("response_id") or data.get("generation_id") or "").strip() or None
        ),
        "sources_truncated": sources_truncated,
    }


def moonshot_kimi_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    prompt = f"Use Kimi built-in web search and answer with source URLs.\n\n{query}"
    prompt += _domain_prompt_suffix(allowed_domains)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]

    last_data: dict[str, Any] | None = None
    raw_sources: list[dict[str, Any]] = []
    queries: list[str] = []
    for _attempt in range(4):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "disabled"},
        }
        data = _post_json(
            provider_label="Kimi",
            url=url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "alysis-code/0.1.0",
            },
            payload=payload,
            timeout_s=timeout_s,
            transport=transport,
            resolver=resolver,
            provider_key="moonshot",
            provider_concurrency_caps=provider_concurrency_caps,
            provider_retry_settings=provider_retry_settings,
            operation="moonshot_kimi_search",
        )
        last_data = data
        raw_sources.extend(_collect_source_dicts(data))
        queries.extend(_collect_queries(data))

        choices = data.get("choices")
        choice = (
            next((item for item in choices if isinstance(item, dict)), None)
            if isinstance(choices, list)
            else None
        )
        message = (
            choice.get("message")
            if isinstance(choice, dict) and isinstance(choice.get("message"), dict)
            else None
        )
        finish_reason = str(choice.get("finish_reason") or "") if isinstance(choice, dict) else ""
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if finish_reason != "tool_calls" or not isinstance(tool_calls, list):
            break

        assistant_message = dict(message)
        messages.append(assistant_message)
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            tool_name = ""
            raw_arguments = "{}"
            if isinstance(function, dict):
                tool_name = str(function.get("name") or "").strip()
                raw_arguments = str(function.get("arguments") or "{}")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"raw_arguments": raw_arguments}
            if isinstance(arguments, dict):
                raw_sources.extend(_collect_source_dicts(arguments))
                queries.extend(_collect_queries(arguments))
            tool_result: Any = (
                arguments if tool_name == "$web_search" else {"error": "unknown tool"}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(tool_call.get("id") or "").strip(),
                    "name": tool_name or "$web_search",
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )
    else:
        raise ProviderWebSearchError("Kimi web_search did not finish after 4 tool-call rounds")

    if last_data is None:
        raise ProviderWebSearchError("Kimi web_search did not return a response")
    answer = _chat_completion_answer(last_data)
    sources, sources_truncated = _dedupe_sources(
        [*raw_sources, *_sources_from_answer_urls(answer)],
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Kimi web_search did not return sources")
    return {
        "query": query,
        "answer": answer,
        "citations": _citations_from_sources(sources),
        "sources": sources,
        "queries": queries or [query],
        "model": str(last_data.get("model") or model),
        "backend": "moonshot_kimi",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(last_data.get("id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def zhipu_web_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    provider_query = query[:70].rsplit(" ", 1)[0].strip() if len(query) > 70 else query
    if not provider_query:
        provider_query = query[:70]
    payload: dict[str, Any] = {
        "search_query": provider_query,
        "search_engine": "search_pro",
        "search_intent": False,
        "count": min(max_results, 50),
        "search_recency_filter": "noLimit",
        "content_size": "medium",
    }
    if allowed_domains and len(allowed_domains) == 1:
        payload["search_domain_filter"] = allowed_domains[0]
    data = _post_json(
        provider_label="Zhipu",
        url=f"{base_url.rstrip('/')}/web_search",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload=payload,
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="zhipu",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="zhipu_web_search",
    )
    raw_results = data.get("search_result")
    raw_sources = (
        [item for item in raw_results if isinstance(item, dict)]
        if isinstance(raw_results, list)
        else []
    )
    sources, sources_truncated = _dedupe_sources(
        raw_sources,
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Zhipu web_search did not return sources")
    return {
        "query": query,
        "answer": "",
        "citations": _citations_from_sources(sources),
        "sources": sources,
        "queries": _collect_queries(data) or [provider_query],
        "model": model,
        "backend": "zhipu_web_search",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or data.get("request_id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def volcengine_web_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    prompt = f"{query}{_domain_prompt_suffix(allowed_domains)}"
    data = _post_json(
        provider_label="Volcengine",
        url=f"{base_url.rstrip('/')}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload={
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search"}],
            "stream": False,
        },
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="volcengine",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="volcengine_web_search",
    )
    answer = _responses_answer(data)
    sources, sources_truncated = _dedupe_sources(
        [*_collect_source_dicts(data), *_sources_from_answer_urls(answer)],
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Volcengine web_search did not return sources")
    return {
        "query": query,
        "answer": answer,
        "citations": _citations_from_sources(sources),
        "sources": sources,
        "queries": _collect_queries(data) or [query],
        "model": str(data.get("model") or model),
        "backend": "volcengine_web_search",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def anthropic_messages_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/messages"
    tool: dict[str, Any] = {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 3,
    }
    if allowed_domains:
        tool["allowed_domains"] = list(allowed_domains)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 2048,
        "messages": [
            {
                "role": "user",
                "content": f"Use web search to answer with citations.\n\n{query}",
            }
        ],
        "tools": [tool],
    }
    data = _post_json(
        provider_label="Anthropic",
        url=url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload=payload,
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="anthropic",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="anthropic_web_search",
    )
    content = data.get("content")
    if not isinstance(content, list):
        raise ProviderWebSearchError("Unexpected Anthropic payload: missing content list")

    text_parts: list[str] = []
    raw_sources: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    queries: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            for citation in (
                block.get("citations") if isinstance(block.get("citations"), list) else []
            ):
                source = _normalize_source(citation)
                if source is None:
                    continue
                source["start_index"] = citation.get("start_index")
                source["end_index"] = citation.get("end_index")
                raw_sources.append(source)
                citations.append(
                    {
                        "title": str(source.get("title") or "").strip(),
                        "url": str(source.get("url") or "").strip(),
                        "start_index": source.get("start_index"),
                        "end_index": source.get("end_index"),
                    }
                )
        elif block_type == "server_tool_use":
            raw_input = block.get("input")
            if isinstance(raw_input, dict):
                raw_query = str(raw_input.get("query") or "").strip()
                if raw_query:
                    queries.append(raw_query)
        elif block_type == "web_search_tool_result":
            results = block.get("content")
            if isinstance(results, list):
                raw_sources.extend(result for result in results if isinstance(result, dict))

    answer = "".join(text_parts).strip()
    sources, sources_truncated = _dedupe_sources(
        raw_sources,
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Anthropic web_search did not return sources")
    citations = _finalize_citations(citations, sources, max_results=max_results)
    return {
        "query": query,
        "answer": answer,
        "citations": citations,
        "sources": sources,
        "queries": queries or [query],
        "model": str(data.get("model") or model),
        "backend": "anthropic_messages",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def _gemini_native_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/openai"):
        return normalized.removesuffix("/openai")
    return normalized


def gemini_grounding_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    encoded_model = quote(model, safe="")
    url = f"{_gemini_native_base_url(base_url)}/models/{encoded_model}:generateContent"
    prompt = f"{query}{_domain_prompt_suffix(allowed_domains)}"
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
    }
    data = _post_json(
        provider_label="Gemini",
        url=url,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload=payload,
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="gemini",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="gemini_grounding_search",
    )
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ProviderWebSearchError("Gemini grounding payload missing candidates")
    candidate = next((item for item in candidates if isinstance(item, dict)), {})
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    text_parts: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    answer = "".join(text_parts).strip()
    grounding = candidate.get("groundingMetadata") if isinstance(candidate, dict) else None
    raw_chunks = grounding.get("groundingChunks") if isinstance(grounding, dict) else None
    raw_sources = (
        [chunk for chunk in raw_chunks if isinstance(chunk, dict)]
        if isinstance(raw_chunks, list)
        else []
    )
    sources, sources_truncated = _dedupe_sources(
        raw_sources,
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    citations: list[dict[str, Any]] = []
    supports = grounding.get("groundingSupports") if isinstance(grounding, dict) else None
    if isinstance(supports, list) and isinstance(raw_chunks, list):
        for support in supports:
            if not isinstance(support, dict):
                continue
            segment = support.get("segment") if isinstance(support.get("segment"), dict) else {}
            for raw_index in support.get("groundingChunkIndices", []):
                try:
                    chunk = raw_chunks[int(raw_index)]
                except (TypeError, ValueError, IndexError):
                    continue
                source = _normalize_source(chunk)
                if source is None:
                    continue
                if not _host_matches_allowed_domains(str(source.get("url") or ""), allowed_domains):
                    continue
                citations.append(
                    {
                        "title": str(source.get("title") or "").strip(),
                        "url": str(source.get("url") or "").strip(),
                        "start_index": segment.get("startIndex"),
                        "end_index": segment.get("endIndex"),
                    }
                )
    queries = []
    raw_queries = grounding.get("webSearchQueries") if isinstance(grounding, dict) else None
    if isinstance(raw_queries, list):
        queries = [str(item).strip() for item in raw_queries if str(item).strip()]
    if not sources:
        raise ProviderWebSearchError("Gemini grounding did not return sources")
    citations = _finalize_citations(citations, sources, max_results=max_results)
    return {
        "query": query,
        "answer": answer,
        "citations": citations,
        "sources": sources,
        "queries": queries or [query],
        "model": model,
        "backend": "gemini_grounding",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("responseId") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def openrouter_web_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": "openrouter:web_search",
        "parameters": {"engine": "auto", "max_results": max_results},
    }
    if allowed_domains:
        tool["parameters"]["allowed_domains"] = list(allowed_domains)
    data = _post_json(
        provider_label="OpenRouter",
        url=f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": f"Use web search when useful and cite sources.\n\n{query}",
                }
            ],
            "tools": [tool],
        },
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="openrouter",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="openrouter_web_search",
    )
    answer = _chat_completion_answer(data)
    annotation_sources, citations = _extract_chat_annotations(data)
    sources, sources_truncated = _dedupe_sources(
        [*annotation_sources, *_sources_from_answer_urls(answer)],
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("OpenRouter web_search did not return sources")
    citations = _finalize_citations(citations, sources, max_results=max_results)
    return {
        "query": query,
        "answer": answer,
        "citations": citations,
        "sources": sources,
        "queries": [query],
        "model": str(data.get("model") or model),
        "backend": "openrouter_web",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def perplexity_sonar_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    """Grounded search through Perplexity's Agent API (``POST /v1/agent``).

    Sonar Chat Completions (``/v1/sonar``, models ``sonar`` / ``sonar-pro``)
    shuts down 2026-09-27. The Agent API is Responses-format: the prompt goes
    in ``input``, grounding is opt-in via the ``web_search`` tool (domain
    filters live in the tool's ``filters``), and the reply is a typed
    ``output`` array carrying a ``search_results`` item and a ``message`` item.
    """
    if model in _PERPLEXITY_RETIRED_SONAR_IDS:
        model = _PERPLEXITY_DEFAULT_SEARCH_MODEL
    web_search_tool: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": "low",
        "max_results": max(1, min(int(max_results), 20)),
    }
    if allowed_domains:
        web_search_tool["filters"] = {"search_domain_filter": list(allowed_domains)}
    payload: dict[str, Any] = {
        "model": model,
        "input": query,
        "tools": [web_search_tool],
    }
    url = _perplexity_agent_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "alysis-code/0.1.0",
    }
    if (urlsplit(url).hostname or "").rstrip(".").casefold() == _PERPLEXITY_API_HOST:
        headers["X-Pplx-Integration"] = _PERPLEXITY_INTEGRATION
    data = _post_json(
        provider_label="Perplexity",
        url=url,
        headers=headers,
        payload=payload,
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="perplexity",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="perplexity_sonar_search",
    )
    answer, raw_sources = _perplexity_agent_answer_and_sources(data)
    sources, sources_truncated = _dedupe_sources(
        raw_sources,
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Perplexity did not return sources")
    return {
        "query": query,
        "answer": answer,
        "citations": _citations_from_sources(sources),
        "sources": sources,
        "queries": [query],
        "model": str(data.get("model") or model),
        "backend": "perplexity_sonar",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


_PERPLEXITY_DEFAULT_SEARCH_MODEL = "perplexity/sonar"
# Sonar Chat Completions ids; they are not valid Agent API model ids.
_PERPLEXITY_RETIRED_SONAR_IDS: frozenset[str] = frozenset(
    {"sonar", "sonar-pro", "sonar-reasoning", "sonar-reasoning-pro", "sonar-deep-research"}
)


def _perplexity_agent_url(base_url: str) -> str:
    """Resolve the Agent API endpoint from either the new or the legacy base URL.

    The preset now carries ``https://api.perplexity.ai/v1``; profiles saved
    before the migration carry ``https://api.perplexity.ai``. Both must reach
    ``/v1/agent``.
    """
    root = base_url.rstrip("/")
    if not root.endswith("/v1"):
        root = f"{root}/v1"
    return f"{root}/agent"


def _perplexity_agent_answer_and_sources(
    data: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Extract the answer text and search results from a Responses-format body."""
    answer_parts: list[str] = []
    raw_sources: list[dict[str, Any]] = []
    output = data.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "message":
            content = item.get("content")
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if str(part.get("type") or "") in {"output_text", "text"}:
                    text = str(part.get("text") or "").strip()
                    if text:
                        answer_parts.append(text)
            continue
        results = item.get("results")
        if item_type == "search_results" or isinstance(results, list):
            for result in results if isinstance(results, list) else []:
                if not isinstance(result, dict):
                    continue
                url = str(result.get("url") or "").strip()
                if not url:
                    continue
                raw_sources.append(
                    {
                        "url": url,
                        "title": str(result.get("title") or "").strip(),
                        "snippet": str(result.get("snippet") or "").strip(),
                        "date": str(result.get("date") or "").strip(),
                    }
                )
    fallback_text = str(data.get("output_text") or "").strip()
    answer = "\n\n".join(answer_parts).strip() or fallback_text
    return answer, raw_sources


def groq_compound_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
    }
    if allowed_domains:
        payload["search_settings"] = {"include_domains": list(allowed_domains)}
    data = _post_json(
        provider_label="Groq",
        url=f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload=payload,
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="groq",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="groq_compound_search",
    )
    answer = _chat_completion_answer(data)
    message = _chat_message(data)
    raw_sources: list[dict[str, Any]] = []
    executed_tools = message.get("executed_tools")
    if isinstance(executed_tools, list):
        for tool in executed_tools:
            if not isinstance(tool, dict):
                continue
            search_results = tool.get("search_results")
            if isinstance(search_results, dict):
                results = search_results.get("results")
                if isinstance(results, list):
                    raw_sources.extend(item for item in results if isinstance(item, dict))
            elif isinstance(search_results, list):
                raw_sources.extend(item for item in search_results if isinstance(item, dict))
    annotation_sources, citations = _extract_chat_annotations(data)
    sources, sources_truncated = _dedupe_sources(
        [*raw_sources, *annotation_sources, *_sources_from_answer_urls(answer)],
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Groq Compound did not return sources")
    citations = _finalize_citations(citations, sources, max_results=max_results)
    return {
        "query": query,
        "answer": answer,
        "citations": citations,
        "sources": sources,
        "queries": [query],
        "model": str(data.get("model") or model),
        "backend": "groq_compound",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def mistral_conversations_search(
    *,
    query: str,
    base_url: str,
    api_key: str,
    model: str,
    max_results: int,
    allowed_domains: list[str] | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    provider_concurrency_caps: dict[str, int] | None = None,
    provider_retry_settings: ProviderRetrySettings | None = None,
) -> dict[str, Any]:
    prompt = f"{query}{_domain_prompt_suffix(allowed_domains)}"
    payload: dict[str, Any] = {
        "model": model,
        "inputs": prompt,
        "tools": [{"type": "web_search"}],
        "store": False,
    }
    data = _post_json(
        provider_label="Mistral",
        url=f"{base_url.rstrip('/')}/conversations",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        },
        payload=payload,
        timeout_s=timeout_s,
        transport=transport,
        resolver=resolver,
        provider_key="mistral",
        provider_concurrency_caps=provider_concurrency_caps,
        provider_retry_settings=provider_retry_settings,
        operation="mistral_conversations_search",
    )
    outputs = data.get("outputs")
    if not isinstance(outputs, list):
        raise ProviderWebSearchError("Mistral conversations payload missing outputs")
    text_parts: list[str] = []
    raw_sources: list[dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        content = output.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("text"), str):
                    text_parts.append(chunk["text"])
                if chunk.get("url") or chunk.get("uri") or chunk.get("web"):
                    raw_sources.append(chunk)
        if output.get("url") or output.get("uri") or output.get("web"):
            raw_sources.append(output)
    answer = "".join(text_parts).strip()
    sources, sources_truncated = _dedupe_sources(
        [*raw_sources, *_sources_from_answer_urls(answer)],
        allowed_domains=allowed_domains,
        max_sources=max_results,
    )
    if not sources:
        raise ProviderWebSearchError("Mistral web_search did not return sources")
    return {
        "query": query,
        "answer": answer,
        "citations": _citations_from_sources(sources),
        "sources": sources,
        "queries": [query],
        "model": model,
        "backend": "mistral_conversations",
        "allowed_domains": allowed_domains or [],
        "external_web_access": True,
        "response_id": str(data.get("conversation_id") or "").strip() or None,
        "sources_truncated": sources_truncated,
    }


def normalize_provider_base_url(base_url: str) -> str:
    split = urlsplit(str(base_url or "").strip())
    if not split.scheme or not split.netloc:
        return str(base_url or "").strip().rstrip("/")
    path = split.path.rstrip("/")
    return urlunsplit((split.scheme, split.netloc, path, "", "")).rstrip("/")


def best_effort_search_provider_key(base_url: str, model: str) -> str:
    return best_effort_provider_key(base_url=base_url, model=model)
