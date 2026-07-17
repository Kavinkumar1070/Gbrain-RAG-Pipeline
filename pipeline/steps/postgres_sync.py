"""
Ingest step 7: sync to Postgres.

Mirrors the composed Page (step 5) into the DB, using the content_hash from
step 6 to skip re-sync/re-embed of the *derived* side when nothing actually
changed on disk.

  pages             : upsert (one row per entity, keyed by file_path)
  facts             : INSERT ... ON CONFLICT DO NOTHING, keyed on
                      (page_id, lower(fact)) -- append-only, never deleted.
                      page_write's fact list is the full current set
                      (existing + new, deduped), so a fact already on record
                      just no-ops here instead of duplicating or losing its
                      original source_doc.
  timeline_entries  : same ON CONFLICT DO NOTHING pattern, keyed on
                      (page_id, COALESCE(event_date,''), lower(event)).
  content_chunks    : split by chunk_type.
                        - compiled_truth/fact/timeline ("derived") chunks are
                          fully rebuilt every sync that changes the page --
                          nothing unique lives only in these rows, they're a
                          search-index mirror of pages/facts/timeline_entries.
                        - 'raw' chunks are chunked+embedded from the raw
                          sidecar text and are append-only, one set per
                          ingested doc (keyed by source_doc), never deleted --
                          this is the only place the untouched source text
                          (anything the LLM pass dropped or paraphrased) is
                          searchable at all.

Raw-chunk sync runs whenever raw_text/raw_rel_path are supplied, independent
of whether the derived page content changed -- re-ingesting a doc that
happens to produce an identical compiled_truth/facts/timeline still adds a
*new* raw source doc that wasn't searchable before.

  events            : one row inserted every single call to sync(), no
                      exceptions and no dedup. Unlike everything else in
                      this file, this is NOT gated on content_changed or
                      on anything being newly written -- it's the record
                      that this entity was touched at all on this ingest
                      run, independent of whether that touch produced
                      anything. See init.sql's events table comment.
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

# raw text chunk target size. Paragraph-based: combine consecutive paragraphs
# up to this many characters, splitting any single paragraph that alone
# exceeds it. This is what closes the "one giant unsplit chunk" gap -- a
# whole raw doc used to go into content_chunks as a single row.
RAW_CHUNK_TARGET_CHARS = 1200


@dataclass
class SyncResult:
    page_id: str
    event_id: str           # events row written this run -- always set, even when skipped=True
    skipped: bool          # True if no *derived* content changed (page/facts/timeline/raw chunks).
                            # An events row is still written every run regardless -- skipped never
                            # means "nothing happened," only "nothing new to show for it."
    facts_written: int     # newly inserted facts this run (0 if all already on record)
    timeline_written: int  # newly inserted timeline entries this run
    chunks_written: int    # derived + raw chunks written this run


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None)


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


def _build_derived_chunks(page: Page) -> list[tuple[str, str]]:
    """
    Derived chunk strategy (unchanged from before): one chunk for
    compiled_truth (the narrative), one chunk per fact (already atomic --
    good retrieval granularity), one chunk per timeline entry. No fixed-size
    splitting needed here; compiled_truth is capped at 3-6 sentences by the
    merge skill.

    Returns (text, chunk_type) pairs so the caller can tag each row.
    """
    chunks: list[tuple[str, str]] = []
    if page.compiled_truth.strip():
        chunks.append((page.compiled_truth.strip(), "compiled_truth"))
    chunks.extend((f.strip(), "fact") for f in page.facts if f.strip())
    for t in page.timeline:
        date_str = t.date if t.date else "undated"
        chunks.append((f"{date_str}: {t.event}", "timeline"))
    return chunks


def _chunk_raw_text(raw_text: str, target_chars: int = RAW_CHUNK_TARGET_CHARS) -> list[str]:
    """
    Paragraph-based chunker for raw sidecar text. Combines consecutive
    paragraphs (split on blank lines) up to ~target_chars, so a chunk never
    crosses a paragraph boundary mid-thought unless the paragraph itself is
    too long to fit alone -- in which case it's split on sentence boundaries
    as a fallback.
    """
    raw_text = raw_text.strip()
    if not raw_text:
        return []

    paragraphs = [p.strip() for p in raw_text.split("\n\n") if p.strip()]

    def _split_long_paragraph(paragraph: str) -> list[str]:
        # sentence-boundary fallback for a single paragraph longer than target_chars
        sentences = [s.strip() for s in paragraph.replace("\n", " ").split(". ") if s.strip()]
        pieces, current = [], ""
        for s in sentences:
            candidate = f"{current} {s}." if current else f"{s}."
            if len(candidate) > target_chars and current:
                pieces.append(current.strip())
                current = f"{s}."
            else:
                current = candidate
        if current.strip():
            pieces.append(current.strip())
        return pieces or [paragraph]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > target_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_paragraph(para))
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > target_chars and current:
            chunks.append(current)
            current = para
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


def _sync_raw_chunks(cur, page_id: str, raw_text: str | None, raw_rel_path: str | None) -> int:
    """
    Chunk + embed the raw sidecar text and insert as chunk_type='raw',
    tagged with source_doc=raw_rel_path. Skips entirely if this source_doc
    has already been synced (idempotent re-run of the same ingest), and
    never deletes existing raw chunks -- they're append-only, one set per
    ingested doc.
    """
    if not raw_text or not raw_rel_path:
        return 0

    cur.execute(
        "SELECT 1 FROM content_chunks WHERE source_doc = %s AND chunk_type = 'raw' LIMIT 1",
        (raw_rel_path,),
    )
    if cur.fetchone() is not None:
        return 0

    raw_chunk_texts = _chunk_raw_text(raw_text)
    if not raw_chunk_texts:
        return 0

    embeddings = _embed(raw_chunk_texts)
    for text, vec in zip(raw_chunk_texts, embeddings):
        cur.execute(
            """
            INSERT INTO content_chunks (page_id, chunk_text, embedding, chunk_type, source_doc)
            VALUES (%s, %s, %s, 'raw', %s)
            """,
            (page_id, text, vec, raw_rel_path),
        )
    return len(raw_chunk_texts)


def _sync_event(
    cur,
    entity_id: str,
    page_id: str | None,
    raw_rel_path: str | None,
    content_changed: bool,
    facts_written: int,
    timeline_written: int,
) -> str:
    """
    Record that this ingest run touched the entity -- fires every run,
    regardless of whether it produced any new facts/timeline rows. This is
    what makes "touched, nothing new" distinguishable from "never touched"
    (see init.sql's events table comment for why timeline_entries alone
    can't answer that).

    Deliberately NOT append-only-deduped like facts/timeline_entries: two
    ingest runs of the same source doc are two real, distinct enrichment
    attempts and both should show up in the ledger, even if the second one
    changes nothing. If that turns out to be noisy in practice (e.g. a
    retry loop hammering the same doc), rate-limit at the call site rather
    than collapsing rows here -- collapsing would re-introduce the same
    "can't tell touched-but-empty from untouched" gap this table exists to
    close.
    """
    cur.execute(
        """
        INSERT INTO events (entity_id, page_id, source_doc, content_changed, facts_written, timeline_written)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (entity_id, page_id, raw_rel_path, content_changed, facts_written, timeline_written),
    )
    return str(cur.fetchone()[0])


def sync(
    page: Page,
    commit_result: CommitResult,
    raw_text: str | None = None,
    raw_rel_path: str | None = None,
) -> SyncResult:
    with _conn() as conn:
        with conn.cursor() as cur:
            if commit_result.changed:
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

                # append-only facts: a fact already on record (same page_id,
                # case-insensitive text) no-ops instead of duplicating or
                # losing its original source_doc/created_at.
                facts_written = 0
                for f in page.facts:
                    cur.execute(
                        """
                        INSERT INTO facts (page_id, fact, source_doc)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (page_id, lower(fact)) DO NOTHING
                        RETURNING id
                        """,
                        (page_id, f, raw_rel_path),
                    )
                    if cur.fetchone() is not None:
                        facts_written += 1

                # append-only timeline_entries, same pattern
                timeline_written = 0
                for t in page.timeline:
                    cur.execute(
                        """
                        INSERT INTO timeline_entries (page_id, event_date, event, source_doc)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (page_id, COALESCE(event_date, ''), lower(event)) DO NOTHING
                        RETURNING id
                        """,
                        (page_id, t.date, t.event, raw_rel_path),
                    )
                    if cur.fetchone() is not None:
                        timeline_written += 1

                # derived content_chunks are safe to fully rebuild -- nothing
                # unique lives only here. Raw chunks are untouched (chunk_type != 'raw').
                cur.execute(
                    "DELETE FROM content_chunks WHERE page_id = %s AND chunk_type != 'raw'",
                    (page_id,),
                )
                derived_pairs = _build_derived_chunks(page)
                derived_texts = [text for text, _ in derived_pairs]
                embeddings = _embed(derived_texts)
                for (text, chunk_type), vec in zip(derived_pairs, embeddings):
                    cur.execute(
                        """
                        INSERT INTO content_chunks (page_id, chunk_text, embedding, chunk_type)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (page_id, text, vec, chunk_type),
                    )
                derived_chunks_written = len(derived_pairs)
            else:
                # page content unchanged -- page_id already exists, look it up
                cur.execute("SELECT id FROM pages WHERE file_path = %s", (page.file_path,))
                row = cur.fetchone()
                page_id = row[0] if row else None
                facts_written = 0
                timeline_written = 0
                derived_chunks_written = 0

            # raw chunks sync independent of whether the derived page
            # changed -- see module docstring. Requires a page_id, which
            # should always exist by this point (either just upserted, or
            # found above since an unchanged page implies it was synced
            # before).
            raw_chunks_written = 0
            if page_id is not None:
                raw_chunks_written = _sync_raw_chunks(cur, page_id, raw_text, raw_rel_path)

            # events: fires every call, no gating, no dedup -- this is the
            # "we touched this entity" record that used to be lost entirely
            # whenever commit_result.changed was False and nothing new came
            # out of raw-chunk sync either (see module docstring + init.sql).
            # Uses page.entity_id (always set on Page) rather than page_id,
            # since page_id can in principle be None here if a page row
            # somehow doesn't exist yet -- the event should still be logged
            # against the entity even in that edge case.
            event_id = _sync_event(
                cur,
                entity_id=page.entity_id,
                page_id=page_id,
                raw_rel_path=raw_rel_path,
                content_changed=commit_result.changed,
                facts_written=facts_written,
                timeline_written=timeline_written,
            )

            conn.commit()

    skipped = (
        not commit_result.changed
        and facts_written == 0
        and timeline_written == 0
        and raw_chunks_written == 0
    )

    return SyncResult(
        page_id=str(page_id) if page_id else page.entity_id,
        event_id=event_id,
        skipped=skipped,
        facts_written=facts_written,
        timeline_written=timeline_written,
        chunks_written=derived_chunks_written + raw_chunks_written,
    )


if __name__ == "__main__":
    print("This step runs as part of ingest.py — see steps/ingest.py for a full pipeline run.")