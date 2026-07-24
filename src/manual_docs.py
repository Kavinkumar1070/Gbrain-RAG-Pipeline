"""Stand-in for paid enrichment APIs (Crustdata, Crunchbase, etc.).

Drop a plain-text file named after the entity into enrichment_docs/, e.g.:
    enrichment_docs/sarah-chen.txt
    enrichment_docs/acme.txt

When enrich hits a brand-new entity, it looks here first -- same position in the
pipeline an API call would occupy, same output shape (structured facts), just
human-written instead of JSON from a vendor. This is explicitly the *highest value*
source per gbrain's own doc ("your own interactions... highest signal"), not a
lesser substitute for the real thing.
"""
import os
from rapidfuzz import fuzz

from src import config
from src.azure_client import chat

_EXTRACT_FACTS_PROMPT = """Read these freeform notes about a person or company and extract
structured facts as JSON: {"role": "...", "company": "...", "summary": "...", "beliefs": "...", "other": "..."}
Only include fields you actually find information for -- omit the rest. Output ONLY JSON, no prose.
"""


def _list_docs() -> list[str]:
    if not os.path.isdir(config.MANUAL_DOCS_DIR):
        return []
    return [f for f in os.listdir(config.MANUAL_DOCS_DIR) if f.endswith((".txt", ".md"))]


def lookup_manual_doc(entity_name: str, threshold: float = 80) -> dict | None:
    """Fuzzy-matches entity_name against filenames in enrichment_docs/. Returns extracted
    facts dict, or None if no matching file exists."""
    docs = _list_docs()
    if not docs:
        return None

    target = entity_name.lower().replace(" ", "-")
    best_file, best_score = None, 0
    for fname in docs:
        stem = os.path.splitext(fname)[0].lower()
        # Substring containment first -- catches "Sarah" -> "sarah-chen.txt"
        # (transcripts often only ever use a first name; fuzz.ratio on the
        # full strings scores that ~67, below the 80 threshold, and the
        # manual doc silently never gets read). Whole-string fuzzy ratio is
        # still needed for typos/spelling variants where neither is a
        # substring of the other.
        if len(target) >= 3 and (target in stem or stem in target):
            score = 100
        else:
            score = fuzz.ratio(target, stem)
        if score > best_score:
            best_score, best_file = score, fname

    if best_score < threshold or best_file is None:
        return None

    path = os.path.join(config.MANUAL_DOCS_DIR, best_file)
    with open(path, encoding="utf-8") as f:
        raw_notes = f.read()

    if not raw_notes.strip():
        return None

    import json
    result = chat(system=_EXTRACT_FACTS_PROMPT, user=raw_notes, json_mode=True, temperature=0.1)
    try:
        facts = json.loads(result)
    except json.JSONDecodeError:
        return None
    facts["_source_file"] = path
    return facts