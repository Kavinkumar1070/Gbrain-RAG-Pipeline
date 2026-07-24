"""Entrypoint. Exactly three LLM calls happen in this whole pipeline, each a
single skill (a markdown file as system prompt) in, structured JSON out:

  1. skills/RESOLVER.md            -- which of the 2 extraction skills handles this signal
  2. skills/<skill>/SKILL.md       -- entities/facts/relationships from the text
  3. skills/compose-page/SKILL.md  -- page prose (called inside render_md.py)

Everything else is plain deterministic Python:
  - file classification & text extraction  (signal_detector.py, source_reader.py)
  - dedupe / "does this entity already exist"  (dedupe.py)
  - entity creation, facts, events, relationships, tier escalation  (db.py)
  - manual-doc lookup  (manual_docs.py)
  - writing the .md file to disk  (render_md.py)
  - git commit  (git_commit.py)

The model never gets a tool, never touches the DB, and never touches the
filesystem directly. It reads text, returns JSON, and Python decides what
happens next.

Every step below prints what it actually did -- this is "the enrich step,"
made visible. There's no hidden agent decision; every line printed
corresponds to one real function call.

Usage:
    python run_pipeline.py sources/2026-07-23-product-review.txt
"""
import sys

from src import config, db
from src.azure_client import embed
from src.chunker import chunk_text
from src.dedupe import find_entity
from src.git_commit import commit_wiki
from src.manual_docs import lookup_manual_doc
from src.render_md import render_entity_page
from src.signal_detector import classify as classify_file_type
from src.skill_runner import run_skill
from src.source_reader import detect_mime, hash_file, read_source

VALID_SKILLS = {"meeting-ingestion", "media-ingest"}

# Used only if the routing skill call fails outright -- keeps the pipeline
# running on a deterministic default rather than crashing.
FILE_TYPE_FALLBACK_SKILL = {
    "pdf": "media-ingest",
    "transcript": "meeting-ingestion",
    "txt": "media-ingest",
}


def route_signal(text: str, file_type: str) -> str:
    """LLM call #1. Falls back to the deterministic file-type mapping if the
    model call errors or returns something outside the valid set."""
    result = run_skill("skills/RESOLVER.md", text[:4000], json_mode=True)
    skill = (result or {}).get("skill")
    if skill in VALID_SKILLS:
        return skill
    print(f"[route] skill call returned {result!r}, falling back to file-type mapping")
    return FILE_TYPE_FALLBACK_SKILL[file_type]


def extract_entities(skill: str, text: str) -> list[dict]:
    """LLM call #2. Returns [] (not an exception) on a bad/empty response --
    an ingest run that finds nothing is a valid outcome, not a crash."""
    result = run_skill(f"skills/{skill}/SKILL.md", text, json_mode=True)
    return (result or {}).get("entities", [])


def get_or_create_entity(name: str, entity_type: str, indent: str = "  ") -> str:
    """Dedupe + create. No LLM call -- find_entity is fuzzy match + embedding
    similarity, both deterministic once the embedding itself exists.
    This is the function most worth watching: it's the whole "is this new or
    do I already know them" decision, and it's plain Python, not a model call."""
    entity_id, matched_via_alias = find_entity(name, entity_type)

    if entity_id is None:
        entity_id = db.create_entity(name, entity_type, embed(name))
        print(f"{indent}[new] {entity_type} '{name}' -> created ({entity_id})")

        manual = lookup_manual_doc(name)
        if manual:
            fields = [k for k in manual if k != "_source_file"]
            for field in fields:
                db.insert_fact(entity_id, field, str(manual[field]), source="manual_doc", confidence="self-described")
            print(f"{indent}[enrich] manual doc matched ({manual.get('_source_file')}) "
                  f"-> wrote {len(fields)} fact(s): {fields}")
        else:
            print(f"{indent}[enrich] no manual doc match -- created from source text alone")

    elif matched_via_alias:
        db.add_alias(entity_id, name)
        print(f"{indent}[match] '{name}' matched existing {entity_type} ({entity_id}) via alias -- alias recorded")
    else:
        print(f"{indent}[match] '{name}' matched existing {entity_type} ({entity_id}) via canonical name")

    return entity_id


def escalate_tier(entity_id: str, indent: str = "  "):
    n = db.count_events(entity_id)
    before = db.get_entity(entity_id)["tier"]
    if n >= config.TIER1_EVENT_COUNT:
        db.set_tier(entity_id, 1)
    elif n >= config.TIER2_EVENT_COUNT:
        db.set_tier(entity_id, 2)
    after = db.get_entity(entity_id)["tier"]
    if after != before:
        print(f"{indent}[tier] {n} events -> escalated tier {before} -> {after}")
    else:
        print(f"{indent}[tier] {n} events -> tier stays {after}")


def process(filepath: str):
    # File-level ingest dedup, by content -- not by path. Hashing raw bytes
    # (not extracted text) means a renamed/copied duplicate is still caught,
    # and a byte-identical re-run of the same path is always caught. Checked
    # BEFORE any LLM calls or DB writes so a duplicate costs nothing.
    content_hash = hash_file(filepath)
    mime_type = detect_mime(filepath)
    existing = db.get_source_by_hash(content_hash)
    if existing:
        print(f"[skip] {filepath} ({mime_type}) already ingested as {existing['filepath']!r} "
              f"on {existing['ingested_at']} (hash {content_hash[:12]}...)")
        return

    file_type = classify_file_type(filepath)
    text = read_source(filepath, file_type)

    skill = route_signal(text, file_type)
    print(f"[route] {filepath} ({file_type}) -> {skill}")

    entities = extract_entities(skill, text)
    if not entities:
        print("[extract] no entities found -- nothing to persist")
        # Still a completed, valid ingest (not a failure) -- record it so a
        # re-run doesn't redo the routing/extraction LLM calls for nothing.
        db.record_source(content_hash, mime_type, filepath)
        return
    print(f"[extract] found {len(entities)} entities: {[e['name'] for e in entities]}")

    touched_ids: set[str] = set()

    for e in entities:
        name, etype = e["name"], e["type"]

        # Config-driven type enforcement: the skill prompt already tells the
        # model the current ENTITY_TYPES list (see skill_runner.py), but the
        # model can still drift. This is the deterministic backstop -- an
        # entity typed outside what .env currently declares is skipped
        # (entities.type has a matching DB CHECK constraint via
        # db.init_schema(), so an insert would fail anyway; this just fails
        # loudly and early instead of crashing mid-run). Change .env and
        # rerun setup_db.py to recognize a new type -- don't silently widen
        # this check to let unrecognized types through.
        if etype not in config.ENTITY_TYPES:
            print(f"\n[skip] {name} has unrecognized type '{etype}' "
                  f"(ENTITY_TYPES={list(config.ENTITY_TYPES)}) -- not in .env, not persisted")
            continue

        print(f"\n[enrich] {name} ({etype})")
        entity_id = get_or_create_entity(name, etype)
        touched_ids.add(entity_id)

        db.insert_event(
            entity_id,
            source_type=skill,
            source_ref=filepath,
            content=e.get("fact", ""),
            confidence=e.get("confidence", "observed"),
        )
        print(f"  [event] logged: \"{e.get('fact', '')}\" ({e.get('confidence', 'observed')})")

        # Structured facts stated in the source text itself. Previously
        # insert_fact() was only ever called from the manual_doc match inside
        # get_or_create_entity() -- an entity with no matching enrichment_docs/
        # file got NO facts at all, even when the transcript stated something
        # concrete ("starting Acme's platform team on the 15th"). The
        # narrative sentence went to `events` either way; it just never
        # became a structured, queryable fact. This is the second facts
        # source, same table, same shape, just sourced from the skill call
        # instead of a manual doc.
        facts = e.get("facts") or {}
        for field, value in facts.items():
            db.insert_fact(entity_id, field, str(value), source=skill, confidence=e.get("confidence", "observed"))
        if facts:
            print(f"  [fact] {len(facts)} structured fact(s) from source text: {list(facts.keys())}")

        for rel in e.get("related", []):
            if rel["type"] not in config.ENTITY_TYPES:
                print(f"  [skip] related '{rel['name']}' has unrecognized type '{rel['type']}' -- edge not persisted")
                continue
            # relation_type (the edge label, e.g. works_at/attended/knows) is
            # deliberately NOT enforced against config.RELATION_TYPES the way
            # entity type is -- relationships.relation_type is free TEXT in
            # the schema on purpose (see db/schema.sql). RELATION_TYPES is
            # vocabulary guidance fed to the model (skill_runner.py), not a
            # hard constraint, so an edge type outside that list is still
            # written -- just flagged here so you notice vocabulary drift
            # and can decide whether to add it to .env.
            note = "" if rel["relation_type"] in config.RELATION_TYPES else "  (not in configured RELATION_TYPES)"
            rel_id = get_or_create_entity(rel["name"], rel["type"], indent="    ")
            touched_ids.add(rel_id)
            was_new = db.insert_relationship(entity_id, rel_id, rel["relation_type"], source_ref=filepath)
            tag = "[relate]" if was_new else "[relate:dup]"
            suffix = "" if was_new else "  (edge already existed -- not duplicated)"
            print(f"  {tag} {name} --{rel['relation_type']}--> {rel['name']} ({rel['type']}){note}{suffix}")

        escalate_tier(entity_id)

    print()
    paths = [render_entity_page(eid) for eid in touched_ids]
    print(f"[render] wrote {len(paths)} page(s):")
    for p in paths:
        print(f"  - {p}")

    # Chunk + embed the raw source text and store it -- this is ingestion-time
    # population only (fills the `chunks` table for later retrieval). No
    # query/search happens here or anywhere in this pipeline; that's a
    # separate, later piece. source_type='source' keeps this in its own
    # namespace ("what was actually said") from the wiki-page chunks below
    # ("what we currently believe, post-dedupe/compose").
    pieces = chunk_text(text)
    if pieces:
        embeddings = [embed(p) for p in pieces]
        db.replace_chunks(filepath, pieces, embeddings, source_type="source")
        print(f"[chunk] embedded {len(pieces)} chunk(s) from {filepath}")

    # Also chunk + embed each rendered wiki page -- this is what makes the
    # *compiled* knowledge (deduped facts, compose-page summary, relationship
    # backlinks) retrievable, not just the original transcript text. Same two
    # representations per chunk (chunk_embedding + the generated chunk_tsv
    # column), just source_type='wiki_page' and source_ref=the .md path.
    for p in paths:
        with open(p, encoding="utf-8") as f:
            page_text = f.read()
        page_pieces = chunk_text(page_text)
        if page_pieces:
            page_embeddings = [embed(pc) for pc in page_pieces]
            db.replace_chunks(p, page_pieces, page_embeddings, source_type="wiki_page")
            print(f"[chunk] embedded {len(page_pieces)} chunk(s) from {p} (wiki_page)")

    commit_wiki(f"ingest: {filepath} ({skill}) -- {len(touched_ids)} entities touched")

    # Recorded last, only once everything above succeeded -- a run that
    # crashes partway through stays retryable (no source row written), so
    # this dedup can't silently swallow a failed ingest as "already done".
    db.record_source(content_hash, mime_type, filepath)
    print(f"[source] recorded {filepath} as ingested (hash {content_hash[:12]}..., {mime_type})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_pipeline.py <path-to-source-file>")
        sys.exit(1)
    process(sys.argv[1])