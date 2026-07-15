"""
Ingest step 7: sync to Postgres.

Mirrors the composed Page (step 5) into the DB, using the content_hash from
step 6 to skip re-sync/re-embed when nothing actually changed on disk.

  pages     : upsert (one row per entity, keyed by file_path)
  facts     : delete + reinsert for this page_id (page_write's fact list is
              already the full current set, not a diff)
  timeline_entries : delete + reinsert
  content_chunks    : delete + reinsert, chunk + embed compiled_truth/facts/
              timeline so retrieval step 2/3 (vector + keyword search) has
              something to search over. tsv populates via the DB trigger.
"""
import os
from dataclasses import dataclass

import psycopg
from dotenv import load_dotenv
from openai import AzureOpenAI

from steps.page_write import Page
from steps.git_commit import CommitResult

load_dotenv()

EMBEDDING_DEPLOYMENT = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]  # text-embedding-3-small, 1536-dim


@dataclass
class SyncResult:
    page_id: str
    skipped: bool          # True if content_hash unchanged, no DB writes made
    facts_written: int
    timeline_written: int
    chunks_written: int


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


def _embed_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )


def _embed(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of chunk texts. Azure OpenAI embeddings endpoint accepts a list input."""
    if not texts:
        return []
    client = _embed_client()
    response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=texts)
    return [item.embedding for item in response.data]


def _build_chunks(page: Page) -> list[str]:
    """
    Chunk strategy: one chunk for compiled_truth (the narrative), one chunk per
    fact (already atomic — good retrieval granularity), one chunk per timeline
    entry. No fixed-size splitting yet; compiled_truth is capped at 3-6
    sentences by the merge skill so it doesn't need sub-splitting today.
    """
    chunks = []
    if page.compiled_truth.strip():
        chunks.append(page.compiled_truth.strip())
    chunks.extend(f.strip() for f in page.facts if f.strip())
    for t in page.timeline:
        date_str = t.date if t.date else "undated"
        chunks.append(f"{date_str}: {t.event}")
    return chunks


def sync(page: Page, commit_result: CommitResult) -> SyncResult:
    if not commit_result.changed:
        return SyncResult(page_id=page.entity_id, skipped=True,
                           facts_written=0, timeline_written=0, chunks_written=0)

    with _conn() as conn:
        with conn.cursor() as cur:
            # upsert pages row
            cur.execute(
                """
                INSERT INTO pages (entity_id, file_path, content_hash, compiled_truth, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (file_path) DO UPDATE SET
                    content_hash = EXCLUDED.content_hash,
                    compiled_truth = EXCLUDED.compiled_truth,
                    updated_at = now()
                RETURNING id
                """,
                (page.entity_id, page.file_path, commit_result.content_hash, page.compiled_truth),
            )
            page_id = cur.fetchone()[0]

            # replace facts
            cur.execute("DELETE FROM facts WHERE page_id = %s", (page_id,))
            for f in page.facts:
                cur.execute("INSERT INTO facts (page_id, fact) VALUES (%s, %s)", (page_id, f))

            # replace timeline_entries
            cur.execute("DELETE FROM timeline_entries WHERE page_id = %s", (page_id,))
            for t in page.timeline:
                cur.execute(
                    "INSERT INTO timeline_entries (page_id, event_date, event) VALUES (%s, %s, %s)",
                    (page_id, t.date, t.event),
                )

            # replace content_chunks
            cur.execute("DELETE FROM content_chunks WHERE page_id = %s", (page_id,))
            chunk_texts = _build_chunks(page)
            embeddings = _embed(chunk_texts)
            for text, vec in zip(chunk_texts, embeddings):
                cur.execute(
                    "INSERT INTO content_chunks (page_id, chunk_text, embedding) VALUES (%s, %s, %s)",
                    (page_id, text, vec),
                )

            conn.commit()

    return SyncResult(
        page_id=str(page_id),
        skipped=False,
        facts_written=len(page.facts),
        timeline_written=len(page.timeline),
        chunks_written=len(chunk_texts),
    )


if __name__ == "__main__":
    print("This step runs as part of ingest.py — see steps/ingest.py for a full pipeline run.")