# Agent system prompts

Alysis Code builds the main agent's system prompt at session creation. The shared
base text is `SYSTEM_PROMPT` in
[`prompt_context.py`](../src/alysis_code/agent/prompt_context.py). The same
module adds applicable sections for editing, skills, delegation, personas, and
one-shot execution. It also attaches trusted session context separately from
the system prompt. There is no single Markdown file that the runtime loads as
the parent prompt.

## Working guidance

The working guidance is written in
[`prompt_guidance.py`](../src/alysis_code/agent/prompt_guidance.py) and
[`prompt_families.py`](../src/alysis_code/agent/prompt_families.py).
The [exact model catalog](../src/alysis_code/prompt_guidance_catalog.py)
selects a family and a `normal` or `expanded` variant for a parent agent.
Expanded guidance adds concrete execution scaffolding. Both variants keep the
same correctness, preservation, and evidence requirements.

| Parent models | Starting profile |
| --- | --- |
| GPT Astra, Sol, Terra and the reviewed Sol alias | `gpt-normal` |
| GPT Luna, GPT-5.5, GPT-5.4 and older reviewed GPT IDs | `gpt-expanded` |
| Other reviewed model IDs | Family and variant in the exact catalog |
| Unknown or unlisted IDs | General `expanded` fallback |

The catalog uses exact model identities. It does not infer a prompt family from
name substrings, provider names, pricing, or parameter counts. A listed model
does not guarantee account access, availability, or tool suitability. These
assignments are editable starting preferences, not measured rankings.

Change them with, for example:

```bash
alysis config set prompt_guidance.default expanded
alysis config set prompt_guidance.model_profiles.gpt-5.6-luna gpt-expanded
alysis config set prompt_guidance.model_profiles.vendor/model.v2 qwen-normal
alysis config set prompt_guidance.model_profiles.vendor/model.v2 ''
```

The last command removes that exact model assignment. Setting
`prompt_guidance.model_profiles` to a JSON object replaces the whole mapping;
`'{}'` makes every parent use the configured default. Saved assignments remain
in effect until explicitly changed. General `compact`, `balanced`, and
`expanded` overrides remain valid; previously accepted named profiles remain
compatibility aliases for their family and density.

## Child prompts

A subagent uses the family of its own effective model, including any role model
override. Children use one working prompt per family, with their role and
assignment instructions added separately. For example, GPT Sol and GPT Luna
parents can receive different guidance densities while their GPT children both
receive `gpt-subagent`. A general parent override selects
`general-subagent` for children. Disabling delegation on a parent does not
make it a child. See [Subagents](subagents.md) for child roles and execution.

Prompt selection changes instruction text only. It does not change reasoning
effort, models, tools, permissions, concurrency, or delegation availability.
Trusted full system-prompt overrides remain full overrides. Ordinary turns keep
the same prompt; intentional model or configuration changes refresh the known
host guidance without replacing conversation history or appended role and
custom instructions. Stable prompt text may help cache reuse, but it does not
guarantee a provider cache hit.
