from __future__ import annotations

import json
import re
import shlex

from .models import SkillBundle, SkillMatch
from .state import SkillCatalogEntry

SKILL_ADVERTISE_FALLBACK_MAX_CHARS = 16_000
SKILL_ADVERTISE_MAX_ITEMS = 32
SKILL_MATCH_CONTEXT_MAX_CHARS = 2_200
SKILL_MATCH_MAX_ITEMS = 4
EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS = 10_000
EXPLICIT_SKILL_ENTRYPOINT_MAX_CHARS = 8_000
EXPLICIT_SKILL_ARGUMENT_SUBSTITUTION_MAX_CHARS = 1_200
EXPLICIT_SKILL_ARGUMENTS_MAX_CHARS = 480
EXPLICIT_SKILL_POSITIONAL_ARG_MAX_CHARS = 160
EXPLICIT_SKILL_NAME_DISPLAY_MAX_CHARS = 160
EXPLICIT_SKILL_DESCRIPTION_DISPLAY_MAX_CHARS = 240
EXPLICIT_SKILL_SOURCE_SCOPE_DISPLAY_MAX_CHARS = 40
EXPLICIT_SKILL_SOURCE_KIND_DISPLAY_MAX_CHARS = 40
EXPLICIT_SKILL_SOURCE_PATH_DISPLAY_MAX_CHARS = 320


def build_skill_advertise_block(
    *,
    skills: list[SkillBundle] | tuple[SkillBundle, ...],
    skills_auto_invoke: bool,
    max_chars: int = SKILL_ADVERTISE_FALLBACK_MAX_CHARS,
    max_items: int = SKILL_ADVERTISE_MAX_ITEMS,
) -> str | None:
    if not skills:
        return None
    usage_lines = (
        [
            "- Compare all descriptions with the requested outcome and workflow, not shared "
            "steps or concept mentions; honor exclusions. Choose the narrowest fit; broad "
            "skills are fallbacks.",
            "- Honor explicit user skill requests. Read a chosen workflow with skill_read(name) "
            "before relying on it, unless its instructions are already in context. "
            "Otherwise continue directly.",
            "- skill_read only loads instructions; it never executes them. Use "
            "skill_read(name, path) for bundle files.",
        ]
        if skills_auto_invoke
        else [
            "- Skills are optional attachable context, not auto-executed workflows.",
            "- Use skill_read(name) to inspect SKILL.md before relying on a skill.",
            "- Use skill_read(name, path) for specific files under that skill bundle.",
        ]
    )
    lines = [
        "<skill_context>",
        "source: discovered skill bundles",
        "usage:",
        *usage_lines,
        "available_skills:",
    ]
    truncated = False
    for idx, skill in enumerate(skills):
        if idx >= max_items:
            truncated = True
            break
        description = _truncate_text(skill.description, max_chars=140)
        line = (
            f"- {skill.name} | {description} | source={skill.source_scope}/{skill.source_kind} "
            f"| trust={skill.trust_level}"
        )
        projected = "\n".join([*lines, line, "</skill_context>"])
        if len(projected) > max_chars:
            truncated = True
            break
        lines.append(line)
    if truncated:
        lines.append("- ...(truncated)")
    lines.append("</skill_context>")
    return _bounded_block("\n".join(lines).strip() + "\n", max_chars=max_chars)


def build_matched_skill_context(
    *,
    matches: tuple[SkillMatch, ...] | list[SkillMatch],
    max_chars: int = SKILL_MATCH_CONTEXT_MAX_CHARS,
    max_items: int = SKILL_MATCH_MAX_ITEMS,
) -> str | None:
    filtered = [match for match in matches if isinstance(match, SkillMatch)]
    if not filtered:
        return None
    lines = [
        "<matched_skill_context>",
        "source: host lexical matcher",
        "trust: lower_priority_than_system_direct_user_and_explicit_skill_context",
        "These skills may be relevant to the current turn. Read them on demand with skill_read before using them.",
        "matched_skills:",
    ]
    truncated = False
    for idx, match in enumerate(filtered):
        if idx >= max_items:
            truncated = True
            break
        matched_terms = ", ".join(match.matched_terms[:4]) if match.matched_terms else "-"
        line = (
            f"- {match.skill.name} | {_truncate_text(match.skill.description, max_chars=120)} "
            f"| matched={matched_terms}"
        )
        projected = "\n".join([*lines, line, "</matched_skill_context>"])
        if len(projected) > max_chars:
            truncated = True
            break
        lines.append(line)
    if truncated:
        lines.append("- ...(truncated)")
    lines.append("</matched_skill_context>")
    return _bounded_block("\n".join(lines).strip() + "\n", max_chars=max_chars)


def build_explicit_skill_context_message(
    *,
    skill: SkillBundle,
    task_text: str = "",
) -> str:
    resolved_arguments = _resolve_explicit_skill_arguments(task_text)
    placeholder_tokens = _detect_explicit_skill_placeholders(skill.instructions)
    header_fields = _render_explicit_skill_header_fields(skill)
    message = _build_explicit_skill_context_for_mode(
        header_fields=header_fields,
        instructions=skill.instructions,
        raw_task=str(resolved_arguments["raw_task"]),
        positional_args=tuple(resolved_arguments["positional_args"]),
        parser_name=str(resolved_arguments["parser_name"]),
        placeholder_tokens=placeholder_tokens,
        reduce_arguments=False,
        omit_argument_block=False,
    )
    if len(message) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS:
        return message

    reduced_message = _build_explicit_skill_context_for_mode(
        header_fields=header_fields,
        instructions=skill.instructions,
        raw_task=str(resolved_arguments["raw_task"]),
        positional_args=tuple(resolved_arguments["positional_args"]),
        parser_name=str(resolved_arguments["parser_name"]),
        placeholder_tokens=placeholder_tokens,
        reduce_arguments=True,
        omit_argument_block=False,
    )
    if len(reduced_message) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS:
        return reduced_message

    omitted_message = _build_explicit_skill_context_for_mode(
        header_fields=header_fields,
        instructions=skill.instructions,
        raw_task=str(resolved_arguments["raw_task"]),
        positional_args=tuple(resolved_arguments["positional_args"]),
        parser_name=str(resolved_arguments["parser_name"]),
        placeholder_tokens=placeholder_tokens,
        reduce_arguments=True,
        omit_argument_block=True,
    )
    if len(omitted_message) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS:
        return omitted_message

    return omitted_message


def render_skill_info_text(
    skill: SkillBundle,
    *,
    catalog_entry: SkillCatalogEntry | None = None,
) -> str:
    aliases = ", ".join(skill.aliases) if skill.aliases else skill.name
    lifecycle_lines: list[str] = []
    if catalog_entry is not None:
        lifecycle_lines.extend(
            [
                f"enabled: {'yes' if catalog_entry.enabled else 'no'}",
                f"managed: {'yes' if catalog_entry.managed else 'no'}",
            ]
        )
        if catalog_entry.disabled_by:
            lifecycle_lines.append(f"disabled_by: {catalog_entry.disabled_by}")
        record = catalog_entry.install_record
        if record is not None:
            lifecycle_lines.extend(
                [
                    f"install_source_kind: {record.source_kind or '-'}",
                    f"install_source: {record.source or '-'}",
                    f"install_commit: {record.source_commit or '-'}",
                    f"installed_at: {record.installed_at or '-'}",
                ]
            )
    lifecycle_block = "\n".join(f"{line}" for line in lifecycle_lines)
    if lifecycle_block:
        lifecycle_block += "\n"
    return (
        f"name: {skill.name}\n"
        f"description: {skill.description}\n"
        f"aliases: {aliases}\n"
        f"{lifecycle_block}"
        f"source_scope: {skill.source_scope}\n"
        f"source_kind: {skill.source_kind}\n"
        f"source_family: {skill.source_family}\n"
        f"trust_level: {skill.trust_level}\n"
        f"source_path: {skill.source_path.as_posix()}\n"
        f"entry_path: {skill.entry_path.as_posix()}\n\n"
        f"{skill.instructions.strip()}\n"
    )


def _truncate_text(text: str, *, max_chars: int) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_chars:
        return compact
    if max_chars <= 3:
        return compact[:max_chars]
    return compact[: max_chars - 3] + "..."


def _bounded_block(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 18:
        return text[:max_chars]
    return text[: max_chars - 18].rstrip() + "\n...(truncated)\n"


def _resolve_explicit_skill_arguments(task_text: str) -> dict[str, object]:
    raw_task = str(task_text or "")
    parser_name = "shlex.split"
    try:
        parsed = tuple(shlex.split(raw_task, posix=True))
    except ValueError:
        parsed = tuple(raw_task.split())
        parser_name = "whitespace_split_fallback"
    return {
        "raw_task": raw_task,
        "positional_args": parsed,
        "parser_name": parser_name,
    }


def _detect_explicit_skill_placeholders(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in re.finditer(r"\$(ARGUMENTS|\d+)", str(text or "")):
        token = str(match.group(1) or "")
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return tuple(ordered)


def _substitute_explicit_skill_placeholders(
    text: str,
    *,
    raw_task: str,
    positional_args: tuple[str, ...],
) -> str:
    def _replace(match: re.Match[str]) -> str:
        token = str(match.group(1) or "")
        if token == "ARGUMENTS":
            return raw_task
        try:
            index = int(token) - 1
        except ValueError:
            return match.group(0)
        if index < 0:
            return ""
        return positional_args[index] if index < len(positional_args) else ""

    return re.sub(r"\$(ARGUMENTS|\d+)", _replace, str(text or ""))


def _build_explicit_skill_context_for_mode(
    *,
    header_fields: dict[str, str],
    instructions: str,
    raw_task: str,
    positional_args: tuple[str, ...],
    parser_name: str,
    placeholder_tokens: tuple[str, ...],
    reduce_arguments: bool,
    omit_argument_block: bool,
) -> str:
    if reduce_arguments:
        raw_task = _truncate_explicit_argument_value(
            raw_task,
            max_chars=EXPLICIT_SKILL_ARGUMENTS_MAX_CHARS,
        )
        positional_args = tuple(
            _truncate_explicit_argument_value(
                value,
                max_chars=EXPLICIT_SKILL_POSITIONAL_ARG_MAX_CHARS,
            )
            for value in positional_args
        )
    argument_lines, argument_block_reduced = _render_explicit_skill_argument_lines(
        raw_task=raw_task,
        positional_args=positional_args,
        parser_name=parser_name,
        placeholder_tokens=placeholder_tokens,
        omit_argument_block=omit_argument_block,
    )
    substitution_reduced = reduce_arguments and bool(placeholder_tokens)
    instructions_text = _substitute_explicit_skill_placeholders(
        instructions,
        raw_task=raw_task,
        positional_args=positional_args,
    )

    instruction_truncated = False
    notices = ""
    for _ in range(3):
        fixed_chars = _explicit_skill_context_fixed_chars(
            header_fields=header_fields,
            argument_lines=argument_lines,
            notices=notices,
        )
        available_instruction_chars = min(
            EXPLICIT_SKILL_ENTRYPOINT_MAX_CHARS,
            max(
                0,
                EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS - fixed_chars,
            ),
        )
        bounded_instructions, instruction_truncated = _bound_explicit_skill_instructions(
            instructions_text,
            max_chars=available_instruction_chars,
        )
        next_notices = _render_explicit_skill_context_notices(
            instruction_truncated=instruction_truncated,
            substitution_reduced=substitution_reduced,
            argument_block_reduced=argument_block_reduced or omit_argument_block,
            argument_block_omitted=omit_argument_block and bool(placeholder_tokens),
        )
        if next_notices == notices:
            notices = next_notices
            break
        notices = next_notices

    return (
        "<explicit_skill_context>\n"
        "source: user-selected skill for this turn only\n"
        "trust: lower_priority_than_system_and_direct_user_instructions\n"
        f"name: {header_fields['name']}\n"
        f"description: {header_fields['description']}\n"
        f"source_scope: {header_fields['source_scope']}\n"
        f"source_kind: {header_fields['source_kind']}\n"
        f"source_path: {header_fields['source_path']}\n"
        "This skill is attached only for the current turn. It does not persist across future turns.\n"
        "turn_requirement: Apply this selected skill before taking other actions on the next user task.\n"
        "task_binding: Treat this wrapper and the next user message as one bound instruction set.\n"
        f"{argument_lines}"
        "Read additional bundle files with skill_read(name[, path]) before acting if the skill entrypoint references them.\n\n"
        f"{notices}"
        "<skill_instructions>\n"
        f"{bounded_instructions.rstrip()}\n"
        "</skill_instructions>\n"
        "</explicit_skill_context>\n"
    )


def _explicit_skill_context_fixed_chars(
    *,
    header_fields: dict[str, str],
    argument_lines: str,
    notices: str,
) -> int:
    fixed = (
        "<explicit_skill_context>\n"
        "source: user-selected skill for this turn only\n"
        "trust: lower_priority_than_system_and_direct_user_instructions\n"
        f"name: {header_fields['name']}\n"
        f"description: {header_fields['description']}\n"
        f"source_scope: {header_fields['source_scope']}\n"
        f"source_kind: {header_fields['source_kind']}\n"
        f"source_path: {header_fields['source_path']}\n"
        "This skill is attached only for the current turn. It does not persist across future turns.\n"
        "turn_requirement: Apply this selected skill before taking other actions on the next user task.\n"
        "task_binding: Treat this wrapper and the next user message as one bound instruction set.\n"
        f"{argument_lines}"
        "Read additional bundle files with skill_read(name[, path]) before acting if the skill entrypoint references them.\n\n"
        f"{notices}"
        "<skill_instructions>\n"
        "\n"
        "</skill_instructions>\n"
        "</explicit_skill_context>\n"
    )
    return len(fixed)


def _render_explicit_skill_context_notices(
    *,
    instruction_truncated: bool,
    substitution_reduced: bool,
    argument_block_reduced: bool,
    argument_block_omitted: bool,
) -> str:
    if not any(
        (
            instruction_truncated,
            substitution_reduced,
            argument_block_reduced,
            argument_block_omitted,
        )
    ):
        return ""
    reasons: list[str] = []
    if instruction_truncated:
        reasons.append("Attached entrypoint preview was truncated")
    if substitution_reduced:
        reasons.append("placeholder substitution was reduced")
    if argument_block_omitted:
        reasons.append("argument_substitution was omitted")
    elif argument_block_reduced:
        reasons.append("argument_substitution was reduced")
    reason_text = "; ".join(reasons)
    return (
        f"entrypoint_notice: {reason_text} to fit the explicit skill wrapper budget. "
        "The direct user task remains available in the next user message. "
        "Use skill_read(name[, path]) for the full SKILL.md entrypoint or additional bundle files.\n"
    )


def _render_explicit_skill_header_fields(skill: SkillBundle) -> dict[str, str]:
    return {
        "name": _truncate_explicit_metadata_value(
            skill.name,
            max_chars=EXPLICIT_SKILL_NAME_DISPLAY_MAX_CHARS,
        ),
        "description": _truncate_explicit_metadata_value(
            skill.description,
            max_chars=EXPLICIT_SKILL_DESCRIPTION_DISPLAY_MAX_CHARS,
        ),
        "source_scope": _truncate_explicit_metadata_value(
            skill.source_scope,
            max_chars=EXPLICIT_SKILL_SOURCE_SCOPE_DISPLAY_MAX_CHARS,
        ),
        "source_kind": _truncate_explicit_metadata_value(
            skill.source_kind,
            max_chars=EXPLICIT_SKILL_SOURCE_KIND_DISPLAY_MAX_CHARS,
        ),
        "source_path": _truncate_explicit_metadata_value(
            skill.source_path.as_posix(),
            max_chars=EXPLICIT_SKILL_SOURCE_PATH_DISPLAY_MAX_CHARS,
        ),
    }


def _render_explicit_skill_argument_lines(
    *,
    raw_task: str,
    positional_args: tuple[str, ...],
    parser_name: str,
    placeholder_tokens: tuple[str, ...],
    omit_argument_block: bool,
) -> tuple[str, bool]:
    if not placeholder_tokens or omit_argument_block:
        return "", False
    include_arguments = "ARGUMENTS" in placeholder_tokens
    positional_indexes = sorted(
        {int(token) for token in placeholder_tokens if token.isdigit() and int(token) > 0}
    )
    lines = [
        "argument_substitution:",
        f"- parser = {parser_name}",
    ]
    if include_arguments:
        lines.append(f"- $ARGUMENTS = {_json_literal(raw_task)}")
    for index in positional_indexes:
        value = positional_args[index - 1] if index <= len(positional_args) else ""
        lines.append(f"- ${index} = {_json_literal(value)}")
    if positional_indexes:
        lines.append('- missing positional placeholders resolve to ""')
    rendered = "\n".join(lines) + "\n"
    reduced = False
    if len(rendered) > EXPLICIT_SKILL_ARGUMENT_SUBSTITUTION_MAX_CHARS:
        reduced = True
        rendered = (
            _bounded_block(
                rendered.rstrip(),
                max_chars=EXPLICIT_SKILL_ARGUMENT_SUBSTITUTION_MAX_CHARS,
            ).rstrip()
            + "\n"
        )
    return rendered, reduced


def _json_literal(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _truncate_explicit_argument_value(text: str, *, max_chars: int) -> str:
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    if max_chars <= 16:
        return value[:max_chars]
    return value[: max_chars - 16].rstrip() + "...(truncated)"


def _truncate_explicit_metadata_value(text: str, *, max_chars: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    if max_chars <= 16:
        return value[:max_chars]
    return value[: max_chars - 16].rstrip() + "...(truncated)"


def _bound_explicit_skill_instructions(text: str, *, max_chars: int) -> tuple[str, bool]:
    normalized = str(text or "").rstrip()
    bounded = _bounded_block(normalized, max_chars=max(0, max_chars))
    return bounded, bounded != normalized
