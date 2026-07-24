"""Runs a skill as a single structured LLM call. This IS the whole 'agent' in
this codebase -- a skill is a markdown file used as a system prompt. There is
no tool-calling loop, no subagent, and no filesystem access for the model:
it reads text in, returns JSON out, and every subsequent action on that JSON
is plain Python (see pipeline.py). This is deliberate -- the only things that
need a model's judgment are routing, extraction, and page prose; everything
else (dedupe, DB writes, tiering, file writes, git) is deterministic.
"""
import json

from src import config
from src.azure_client import chat


def _apply_type_placeholders(system_prompt: str) -> str:
    """Fills {{ENTITY_TYPES}} / {{RELATION_TYPES}} placeholders in a SKILL.md
    with the current values of config.ENTITY_TYPES / config.RELATION_TYPES
    (i.e. the ENTITY_TYPES / RELATION_TYPES env vars). This is what lets
    skills/*/SKILL.md describe the extraction contract generically
    ('one of {{ENTITY_TYPES}}') instead of hardcoding 'person | company |
    meeting | project | deal' in the prompt text -- change .env, the model
    sees the new type list on the very next run, no prompt edit needed.
    A no-op string.replace if a skill doesn't use the placeholders."""
    entity_types_str = ", ".join(f'"{t}"' for t in config.ENTITY_TYPES)
    relation_types_str = ", ".join(config.RELATION_TYPES)
    return (
        system_prompt
        .replace("{{ENTITY_TYPES}}", entity_types_str)
        .replace("{{RELATION_TYPES}}", relation_types_str)
    )


def run_skill(skill_path: str, user_content: str, json_mode: bool = True, temperature: float = 0.1) -> dict | str | None:
    """Reads skill_path as the system prompt, sends user_content as the single
    user turn, returns parsed JSON (or None if the model didn't return valid
    JSON) when json_mode=True, else the raw text."""
    with open(skill_path, encoding="utf-8") as f:
        system_prompt = f.read()
    system_prompt = _apply_type_placeholders(system_prompt)

    raw = chat(system=system_prompt, user=user_content, json_mode=json_mode, temperature=temperature)

    if not json_mode:
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None