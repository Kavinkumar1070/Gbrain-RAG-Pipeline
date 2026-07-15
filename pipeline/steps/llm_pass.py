"""
Ingest step 3: LLM pass (skill) -> entity + entity_type + facts + take + timeline + wikilinks

Uses Azure OpenAI structured outputs (JSON schema mode) against gpt-5.4.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

SKILL_PATH = Path(__file__).parent.parent / "skills" / "ingest_extract" / "SKILL.md"

EXTRACT_SCHEMA = {
    "name": "extracted_page",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "entity_type": {
                "type": "string",
                "enum": ["person", "company", "product", "place", "event", "concept"],
            },
            "facts": {
                "type": "array",
                "items": {"type": "string"},
            },
            "take": {"type": "string"},
            "timeline": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": ["string", "null"]},
                        "event": {"type": "string"},
                    },
                    "required": ["date", "event"],
                    "additionalProperties": False,
                },
            },
            "wikilinks": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["entity", "entity_type", "facts", "take", "timeline", "wikilinks"],
        "additionalProperties": False,
    },
}


@dataclass
class TimelineEvent:
    date: str | None
    event: str


@dataclass
class ExtractedPage:
    entity: str
    entity_type: str
    facts: list[str]
    take: str
    timeline: list[TimelineEvent]
    wikilinks: list[str]


def _client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )


def _load_skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def run(raw_text: str, merge_context: str | None = None) -> ExtractedPage:
    """
    Run the LLM extraction pass on raw document text.
    merge_context: existing compiled_truth, if this doc updates a known entity (step 4 output).
    """
    client = _client()
    skill_prompt = _load_skill()

    user_content = raw_text
    if merge_context:
        user_content = (
            f"EXISTING COMPILED_TRUTH (reconcile against this):\n{merge_context}\n\n"
            f"---\n\nNEW DOCUMENT TEXT:\n{raw_text}"
        )

    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": skill_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_schema", "json_schema": EXTRACT_SCHEMA},
    )

    data = json.loads(response.choices[0].message.content)

    return ExtractedPage(
        entity=data["entity"],
        entity_type=data["entity_type"],
        facts=data["facts"],
        take=data["take"],
        timeline=[TimelineEvent(**t) for t in data["timeline"]],
        wikilinks=data["wikilinks"],
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python llm_pass.py <extracted_text_file.txt>")
        sys.exit(1)

    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    result = run(text)

    print(f"Entity: {result.entity} ({result.entity_type})\n")
    print(f"Take:\n{result.take}\n")
    print(f"Facts ({len(result.facts)}):")
    for f in result.facts:
        print(f"  - {f}")
    print(f"\nTimeline ({len(result.timeline)}):")
    for t in result.timeline:
        print(f"  - {t.date}: {t.event}")
    print(f"\nWikilinks: {result.wikilinks}")