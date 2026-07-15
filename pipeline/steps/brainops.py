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


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


def lookup(entity_name: str, entity_type: str | None = None) -> BrainOpsResult:
    entity_name = entity_name.strip()

    with _conn() as conn:
        with conn.cursor() as cur:
            # case-insensitive exact match
            cur.execute(
                "SELECT id, entity_type FROM entities WHERE lower(name) = lower(%s)",
                (entity_name,),
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
                )

            entity_id, existing_type = row

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
                "SELECT compiled_truth, file_path FROM pages WHERE entity_id = %s",
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
                )

            compiled_truth, file_path = page_row
            return BrainOpsResult(
                entity_id=str(entity_id),
                entity_name=entity_name,
                entity_type=existing_type,
                exists=True,
                compiled_truth=compiled_truth,
                file_path=file_path,
            )


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