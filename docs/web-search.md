# Web Search

Alysis Code exposes one `web_search` capability to the active model. The model decides whether a
turn needs web evidence from the tool description and system instructions. Alysis Code does not use
a host-side phrase classifier and does not run a search before the model asks for one.

`web_search_mode` selects the transport:

- `auto`: prefer the active provider's hosted search, with a configured external adapter as
  fallback.
- `native`: use only provider-hosted search.
- `external`: use only a model-independent search provider.
- `off`: do not register a search backend.

`web_search_policy=auto` exposes the capability to the model. `off` hides it. The former `always`
value is accepted as a legacy alias for `auto`.

## Provider Coverage

This table tracks the direct provider presets supported by Alysis Code. "Provider-hosted" means the
provider executes search on its infrastructure. It does not necessarily mean the selected chat
model contains its own search engine.

| Provider preset | Hosted adapter | Supported preset models and constraints |
| --- | --- | --- |
| Alysis Code account (hosted MiMo), OpenRouter | `openrouter_web` | OpenRouter's server-side tool works with any routed model. It uses native upstream search where available and a hosted search engine otherwise. The BYOK `Xiaomi MiMo` preset talks to Xiaomi's own endpoint instead and uses the default `auto` adapter (external search). |
| OpenAI | `openai_responses` | GPT models supported by the Responses API web-search tool, including the GPT-5.4/5.5 models in the preset. |
| Anthropic | `anthropic_messages` | Current Claude models in the preset through the Messages API server tool. |
| Google Gemini | `gemini_grounding` | Gemini 2.5 and current Gemini 3.x models through Google Search grounding. |
| Alibaba Qwen | `dashscope_chat` | Qwen 3.7 and 3.6 use the Responses API `web_search` tool. Qwen 3.5 and supported older Qwen aliases use Chat Completions `enable_search`. The preset pins search to `qwen3.7-plus` when a selected coding model has no search support. |
| Zhipu / GLM | `zhipu_web_search` | Uses Zhipu's first-party Web Search API independently of the selected GLM chat model. |
| Moonshot / Kimi | `moonshot_kimi` | Calls Moonshot's standalone Web Search endpoint (`POST /v1/tools/search`, $0.002 per call that returns results). It runs no model and returns results without an answer. It replaced the chat-completions `$web_search` builtin, which Moonshot retired on 2026-10-20. The endpoint has no domain filter, so allowed domains are filtered locally. |
| MiniMax | `minimax_coding_plan` | Uses the Token Plan search endpoint. A Token Plan key is required; ordinary pay-as-you-go model keys are not interchangeable. |
| ByteDance Doubao | `volcengine_web_search` | Uses the Ark Responses API `web_search` tool with supported Doubao Seed models. |
| Groq | `groq_compound` | Uses `groq/compound-mini` as the provider-hosted search system, regardless of the selected Groq chat model. |
| Mistral | `mistral_conversations` | Uses the Conversations API built-in `web_search` tool with Mistral chat models. |
| xAI | `xai_responses` | Uses the xAI Responses API server-side web-search tool with current Grok models. |
| Cohere | `cohere_web_search` | Uses Cohere Platform's hosted v1 `web-search` connector. Cohere marks the connector deprecated; v2 requires a user-defined external tool. |
| Perplexity | `perplexity_sonar` | Uses the Agent API (`POST /v1/agent`, Responses format) with the `web_search` tool on the `perplexity/sonar` route. Sonar Chat Completions shuts down 2026-09-27; profiles saved against the old base URL are redirected to `/v1/agent` automatically. |

The following direct providers do not currently document a provider-hosted web-search API that
Alysis Code can call with the provider key:

- DeepSeek
- 01.AI / Yi
- Cerebras
- Together AI
- Fireworks AI
- Ollama, LM Studio, vLLM, and custom self-hosted endpoints

Their models still get working `web_search` out of the box through the keyless external adapter
described below — no extra key is required. The same model families can also receive hosted
search when used through OpenRouter.

## External Adapters

Two model-independent external adapters back any provider without hosted search, and also serve
as the automatic fallback when a hosted adapter fails at call time:

| Adapter | Key | Notes |
| --- | --- | --- |
| `tavily` | `TAVILY_API_KEY` or `ALYSIS_WEB_SEARCH_API_KEY` | Preferred external backend when a key is configured. |
| `ddgs` | none (keyless) | DuckDuckGo-family metasearch via the bundled `ddgs` package. Always ready, so `web_search` is available for every provider and model with zero search configuration. Best-effort quality; per-IP rate limits can apply. Disable with `ALYSIS_WEB_SEARCH_KEYLESS=0`. |

In `auto` mode the call-time order is: active provider's hosted adapter, then `tavily` (if a key
is set), then `ddgs`. `native` mode never uses external adapters. `external` mode never uses
hosted adapters.

## Failure Semantics

A `web_search`/`web_fetch` failure inside a turn is classified before it reaches the model:

- Recoverable failures — invalid tool arguments (for example `max_sources` out of range) and
  structured rejections that carry retry guidance (for example the `web_fetch` provenance
  rejection with `fetchable_urls`) — are returned to the model as plain errors so it can correct
  its arguments and retry. They do not disable web tools.
- Site-side `web_fetch` outcomes are recoverable too: the site refused automated clients (an
  anti-bot or challenge page, or HTTP 401, 403, 429, 451, 999), stalled after accepting the
  connection, dropped it, or presented a certificate that can't be verified. The result carries
  `remote_site_reason`, plus `blocked_by_remote_site` for refusals. Transcripts render it as a
  neutral `◦ Fetch Web Page · <url> · <reason>` trace line instead of a failure, and the model is
  told to use a different source rather than retry the host. A host that does not exist is also
  recoverable, as a bad URL.
- Unrecoverable failures — backend/connectivity errors after the external fallback chain is
  exhausted, `web_fetch` connect failures, and DNS outages — convert to a non-error
  `tool_unavailable` observation and remove the failing web tool from the tool schema for the
  remainder of the turn, so the model proceeds from the repository instead of retrying a dead
  backend. The sanitized error is recorded in the `web_tool_unavailable` session event, and
  transcripts show the withdrawn call as `⚠ … unavailable` with that cause.

## Configuration

Prefer automatic provider selection:

```bash
alysis config set web_search_policy auto
alysis config set web_search_mode auto
alysis config set web_search_adapter auto
```

Direct providers without hosted search need no additional configuration: the keyless `ddgs`
adapter serves them automatically. For higher-quality external search, optionally configure the
shared Tavily backend in the service environment:

```bash
export ALYSIS_WEB_SEARCH_API_KEY=<tavily-key>
```

`TAVILY_API_KEY` is also accepted. Managed deployments should provision this key once rather than
requiring a separate key from every end user. Set `ALYSIS_WEB_SEARCH_KEYLESS=0` to opt out of
the keyless fallback entirely (for example in locked-down environments).

## Official API References

- [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Anthropic web search](https://docs.anthropic.com/en/docs/build-with-claude/tool-use/web-search-tool)
- [Gemini Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)
- [OpenRouter server-side web search](https://openrouter.ai/docs/guides/features/server-tools/web-search)
- [Qwen web search](https://www.alibabacloud.com/help/en/model-studio/web-search)
- [Zhipu Web Search API](https://docs.bigmodel.cn/api-reference/%E5%B7%A5%E5%85%B7-api/%E7%BD%91%E7%BB%9C%E6%90%9C%E7%B4%A2)
- [Kimi web search](https://platform.kimi.ai/docs/guide/use-web-search)
- [MiniMax Token Plan MCP search](https://platform.minimax.io/docs/guides/token-plan-mcp-guide)
- [Groq Compound built-in tools](https://console.groq.com/docs/compound/built-in-tools)
- [Mistral web search](https://docs.mistral.ai/studio-api/agents/agent-tools/websearch)
- [xAI web search](https://docs.x.ai/developers/tools/web-search)
- [Cohere v1 to v2 web-search migration](https://docs.cohere.com/v2/docs/migrating-v1-to-v2)
- [Perplexity Agent API web search](https://docs.perplexity.ai/docs/agent-api/tools/web-search) and [Sonar migration guide](https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/how-to)
