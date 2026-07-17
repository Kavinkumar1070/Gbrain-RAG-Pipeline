"""
Ingest step 8: graph edges — classify wikilinks into typed relationships.

Two-part step, same shape as llm_pass (extract) + postgres_sync (write):

  run()  -> LLM classifies each wikilink into a fixed link_type, or omits it
            if no real relationship is stated. Pure, no DB writes.
  sync() -> resolves each to_entity name to an entity_id (creating stub
            entities as needed, via brainops), then replaces all edges for
            this entity: delete + reinsert, same policy as facts/timeline.

link_type is a fixed vocabulary (see skills/graph_edges/SKILL.md and the
links table constraint in init.sql) — the LLM must pick from this list,
it cannot invent new types.

Symmetric types (currently just partnered_with) are stored as a single row;
readers checking for a symmetric relationship should match on either
from_entity_id or to_entity_id.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from openai import AzureOpenAI

from steps import brainops

load_dotenv()

SKILL_PATH = Path(__file__).parent.parent / "skills" / "graph_edges" / "SKILL.md"

LINK_TYPES = [
    "works_at",
    "founded",
    "created",
    "invested_in",
    "acquired",
    "partnered_with",
    "located_in",
    "part_of",
    "attended",
    "related_to",
]

GRAPH_EDGE_SCHEMA = {
    "name": "graph_edges",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "to_entity": {"type": "string"},
                        "link_type": {"type": "string", "enum": LINK_TYPES},
                        "confidence": {"type": "string", "enum": ["explicit", "inferred"]},
                        "evidence": {"type": "string"},
                    },
                    "required": ["to_entity", "link_type", "confidence", "evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["edges"],
        "additionalProperties": False,
    },
}


@dataclass
class GraphEdge:
    to_entity: str
    link_type: str
    confidence: str
    evidence: str


@dataclass
class GraphSyncResult:
    from_entity_id: str
    skipped: bool          # True if step was skipped (e.g. no wikilinks, or content unchanged)
    edges_written: int


def _client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None)


def _load_skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def run(entity_name: str, take: str, facts: list[str], wikilinks: list[str]) -> list[GraphEdge]:
    """
    Classify each wikilink into a typed edge, or drop it if the document
    doesn't actually describe a relationship. Reuses step 3's output
    (take/facts/wikilinks) instead of re-reading raw document text.
    """
    if not wikilinks:
        return []

    client = _client()
    skill_prompt = _load_skill()

    user_content = json.dumps({
        "entity": entity_name,
        "take": take,
        "facts": facts,
        "wikilinks": wikilinks,
    })

    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": skill_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_schema", "json_schema": GRAPH_EDGE_SCHEMA},
    )

    data = json.loads(response.choices[0].message.content)
    return [GraphEdge(**e) for e in data["edges"]]


def sync(from_entity_id: str, edges: list[GraphEdge], content_changed: bool = True) -> GraphSyncResult:
    """
    Replace all graph edges for this entity. Gated on content_changed the
    same way postgres_sync is — no point re-classifying/re-writing edges
    for a page that didn't change on this ingest run.
    """
    if not content_changed:
        return GraphSyncResult(from_entity_id=from_entity_id, skipped=True, edges_written=0)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM links WHERE from_entity_id = %s", (from_entity_id,))

            written = 0
            for edge in edges:
                to_entity_id = brainops.get_or_create_entity_id(edge.to_entity)

                if to_entity_id == from_entity_id:
                    continue  # skip accidental self-links

                cur.execute(
                    """
                    INSERT INTO links (from_entity_id, to_entity_id, link_type, confidence, evidence)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (from_entity_id, to_entity_id, edge.link_type, edge.confidence, edge.evidence),
                )
                written += 1

            conn.commit()

    return GraphSyncResult(from_entity_id=from_entity_id, skipped=False, edges_written=written)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python graph_edges.py <entity name>")
        sys.exit(1)

    # quick manual test against an already-ingested entity's latest page data
    # requires the entity to already exist with facts/take available elsewhere;
    # this is just a smoke test for the LLM classification step in isolation
    brain_result = brainops.lookup(sys.argv[1])
    if not brain_result.exists or not brain_result.compiled_truth:
        print(f"No existing page found for '{sys.argv[1]}' — nothing to test against.")
        sys.exit(1)

    print(f"entity_id: {brain_result.entity_id}")
    print(f"compiled_truth: {brain_result.compiled_truth}")