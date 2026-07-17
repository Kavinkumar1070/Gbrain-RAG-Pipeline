"""
Ingest step 4: brain-ops lookup — does this entity already exist?

  yes -> return existing compiled_truth as merge context (for step 3 reconciliation / step 5 write)
  no  -> create the entity row fresh, no prior context

Match strategy: case-insensitive exact match on entities.name.
Fuzzy/alias matching is a later upgrade if name variants become a problem.
"""
import os
from dataclasses import dataclass

import psycopg
from dotenv import load_dotenv

load_dotenv()


@dataclass
class BrainOpsResult:
    entity_id: str
    entity_name: str
    entity_type: str | None     # None for untyped stub entities (e.g. created via wikilinks, step 8)
    exists: bool
    compiled_truth: str | None  # merge context, None if fresh entity
    file_path: str | None       # existing page's .md path, None if fresh entity
    aliases: list[str] = None   # alternate names on record for this entity (empty list, not None, once loaded)
    matched_alias: str | None = None  # set if lookup hit via alias rather than primary name
    existing_facts: list[str] = None          # facts already on record for this page, [] if none/fresh entity
    existing_timeline: list = None            # [(event_date, event), ...] already on record, [] if none/fresh entity


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


def lookup(entity_name: str, entity_type: str | None = None) -> BrainOpsResult:
    entity_name = entity_name.strip()

    with _conn() as conn:
        with conn.cursor() as cur:
            # case-insensitive match on name OR any alias — catches "M. Chen"
            # resolving to the same row as "Maria Chen" if the alias was
            # recorded earlier via add_alias()
            cur.execute(
                """
                SELECT id, entity_type, name, aliases FROM entities
                WHERE lower(name) = lower(%s)
                   OR lower(%s) = ANY (SELECT lower(a) FROM unnest(aliases) AS a)
                """,
                (entity_name, entity_name),
            )
            row = cur.fetchone()

            if row is None:
                # fresh entity — create it now so step 8 (graph edges) can
                # reference it even before the page is written
                cur.execute(
                    "INSERT INTO entities (name, entity_type) VALUES (%s, %s) RETURNING id",
                    (entity_name, entity_type),
                )
                entity_id = cur.fetchone()[0]
                conn.commit()
                return BrainOpsResult(
                    entity_id=str(entity_id),
                    entity_name=entity_name,
                    entity_type=entity_type,
                    exists=False,
                    compiled_truth=None,
                    file_path=None,
                    aliases=[],
                    matched_alias=None,
                    existing_facts=[],
                    existing_timeline=[],
                )

            entity_id, existing_type, canonical_name, existing_aliases = row
            existing_aliases = existing_aliases or []
            # if we matched via alias rather than the canonical name, note it
            # so callers (e.g. page_write) can keep using the canonical name
            matched_alias = entity_name if entity_name.lower() != canonical_name.lower() else None
            entity_name = canonical_name

            # backfill: an entity row that already exists but has no type yet
            # (e.g. a stub created earlier from a wikilink) gets typed the
            # first time we see it as a primary entity with a known type
            if existing_type is None and entity_type is not None:
                cur.execute(
                    "UPDATE entities SET entity_type = %s WHERE id = %s",
                    (entity_type, entity_id),
                )
                conn.commit()
                existing_type = entity_type

            # entity exists — pull its page's compiled_truth as merge context
            cur.execute(
                "SELECT compiled_truth, file_path, id FROM pages WHERE entity_id = %s",
                (entity_id,),
            )
            page_row = cur.fetchone()

            if page_row is None:
                # entity row exists but no page written yet (e.g. created via
                # a wikilink from another doc, not yet ingested directly)
                return BrainOpsResult(
                    entity_id=str(entity_id),
                    entity_name=entity_name,
                    entity_type=existing_type,
                    exists=True,
                    compiled_truth=None,
                    file_path=None,
                    aliases=existing_aliases,
                    matched_alias=matched_alias,
                    existing_facts=[],
                    existing_timeline=[],
                )

            compiled_truth, file_path, page_id = page_row

            # pull existing facts/timeline so page_write can merge against
            # the real current set instead of only compiled_truth -- without
            # this, postgres_sync's delete+reinsert had no way to know what
            # to keep, and every re-ingest of a related doc silently dropped
            # whatever the new doc didn't happen to repeat.
            cur.execute("SELECT fact FROM facts WHERE page_id = %s", (page_id,))
            existing_facts = [row[0] for row in cur.fetchall()]

            cur.execute(
                "SELECT event_date, event FROM timeline_entries WHERE page_id = %s",
                (page_id,),
            )
            existing_timeline = [(row[0], row[1]) for row in cur.fetchall()]

            return BrainOpsResult(
                entity_id=str(entity_id),
                entity_name=entity_name,
                entity_type=existing_type,
                exists=True,
                compiled_truth=compiled_truth,
                file_path=file_path,
                aliases=existing_aliases,
                matched_alias=matched_alias,
                existing_facts=existing_facts,
                existing_timeline=existing_timeline,
            )


def add_alias(entity_id: str, alias: str) -> None:
    """
    Record an alternate name for an existing entity (e.g. "M. Chen" or an
    email address for "Maria Chen"). Future lookup() calls with that alias
    will resolve to this entity_id instead of creating a duplicate.
    No-op if the alias is already on record (case-insensitive).
    """
    alias = alias.strip()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE entities
                SET aliases = aliases || %s
                WHERE id = %s
                  AND NOT (lower(%s) = ANY (SELECT lower(a) FROM unnest(aliases) AS a))
                  AND lower(%s) != lower(name)
                """,
                (alias, entity_id, alias, alias),
            )
            conn.commit()


def get_or_create_entity_id(entity_name: str) -> str:
    """
    Used by step 8 (graph edge extraction) to resolve wikilink targets to
    entity ids, creating stub entities for names that don't have a page yet.
    These stubs get entity_type=NULL until they're later ingested directly
    (lookup()'s backfill path will type them at that point).
    """
    result = lookup(entity_name)
    return result.entity_id


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python brainops.py <entity name>")
        sys.exit(1)

    result = lookup(sys.argv[1])
    print(f"entity_id: {result.entity_id}")
    print(f"entity_type: {result.entity_type}")
    print(f"exists: {result.exists}")
    print(f"compiled_truth: {result.compiled_truth}")
    print(f"file_path: {result.file_path}")