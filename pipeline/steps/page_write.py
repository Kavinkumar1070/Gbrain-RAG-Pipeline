"""
Ingest step 5: page write.

Composes the final Page (compiled_truth + facts + timeline_entries + frontmatter)
from the LLM extraction (step 3) and brain-ops lookup (step 4).

If the entity already exists with prior compiled_truth, runs an LLM-based
merge/dedup pass so the narrative reconciles rather than duplicates.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import AzureOpenAI

from steps.llm_pass import ExtractedPage
from steps.brainops import BrainOpsResult

load_dotenv()

SKILL_PATH = Path(__file__).parent.parent / "skills" / "page_merge" / "SKILL.md"
print(SKILL_PATH)

MERGE_SCHEMA = {
    "name": "merged_page",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "merged_compiled_truth": {"type": "string"},
        },
        "required": ["merged_compiled_truth"],
        "additionalProperties": False,
    },
}


@dataclass
class Page:
    entity_id: str
    entity_name: str
    file_path: str
    compiled_truth: str
    facts: list[str]
    timeline: list  # list of TimelineEvent
    wikilinks: list[str]
    updated_at: str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def _client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )


def _merge_compiled_truth(existing_compiled_truth: str, new_take: str, new_facts: list[str]) -> str:
    client = _client()
    skill_prompt = SKILL_PATH.read_text(encoding="utf-8")

    user_content = json.dumps({
        "existing_compiled_truth": existing_compiled_truth,
        "new_take": new_take,
        "new_facts": new_facts,
    })

    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": skill_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_schema", "json_schema": MERGE_SCHEMA},
    )

    data = json.loads(response.choices[0].message.content)
    return data["merged_compiled_truth"]


def _dedupe_facts(existing_facts: list[str], new_facts: list[str]) -> list[str]:
    """Case-insensitive exact-string dedup. Semantic dedup is a later upgrade."""
    seen = {f.strip().lower() for f in existing_facts}
    merged = list(existing_facts)
    for f in new_facts:
        if f.strip().lower() not in seen:
            merged.append(f)
            seen.add(f.strip().lower())
    return merged


def run(extracted: ExtractedPage, brain_result: BrainOpsResult) -> Page:
    file_path = brain_result.file_path or f"wiki/{_slugify(brain_result.entity_name)}.md"

    if brain_result.exists and brain_result.compiled_truth:
        compiled_truth = _merge_compiled_truth(
            brain_result.compiled_truth, extracted.take, extracted.facts
        )
        # existing facts aren't passed into this step yet (step 4 only
        # returns compiled_truth) — dedup against new_facts only for now.
        # TODO: pass existing facts through brainops once facts table read is wired.
        facts = extracted.facts
    else:
        compiled_truth = extracted.take
        facts = extracted.facts

    return Page(
        entity_id=brain_result.entity_id,
        entity_name=brain_result.entity_name,
        file_path=file_path,
        compiled_truth=compiled_truth,
        facts=facts,
        timeline=extracted.timeline,
        wikilinks=extracted.wikilinks,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def render_markdown(page: Page) -> str:
    """Render the Page as a .md file with YAML frontmatter — this becomes the git source of truth (step 6)."""
    fm_lines = [
        "---",
        f"entity: {page.entity_name}",
        f"entity_id: {page.entity_id}",
        f"updated_at: {page.updated_at}",
        "---",
        "",
    ]

    body = [f"# {page.entity_name}", ""]
    body += ["## Compiled Truth", "", page.compiled_truth, ""]

    body += ["## Facts", ""]
    for f in page.facts:
        body.append(f"- {f}")
    body.append("")

    body += ["## Timeline", ""]
    for t in page.timeline:
        date_str = t.date if t.date else "undated"
        body.append(f"- {date_str}: {t.event}")
    body.append("")

    body += ["## Related", ""]
    for link in page.wikilinks:
        body.append(f"- [[{link}]]")
    body.append("")

    return "\n".join(fm_lines + body)


if __name__ == "__main__":
    # quick manual test: extract + brainops lookup + page write, print markdown
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from steps import extract, llm_pass, brainops

    if len(sys.argv) != 2:
        print("Usage: python page_write.py <file>")
        sys.exit(1)

    raw = extract.extract(sys.argv[1])
    extracted = llm_pass.run(raw)
    brain_result = brainops.lookup(extracted.entity)
    page = run(extracted, brain_result)

    print(render_markdown(page))