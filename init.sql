-- GBrain schema
-- Run: psql "$DATABASE_URL" -f init.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- entities: brain-ops lookup (ingest step 4)
CREATE TABLE IF NOT EXISTS entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- fixed enum of entity kinds, stored as constrained TEXT (not a Postgres ENUM
-- type) so adding a new kind later is just an ALTER ... DROP/ADD CONSTRAINT,
-- not a type migration. NULL allowed for stub entities created via wikilinks
-- (step 8) that haven't been typed yet.
ALTER TABLE entities ADD COLUMN IF NOT EXISTS entity_type TEXT
    CHECK (entity_type IN ('person', 'company', 'product', 'place', 'event', 'concept'));

-- alternate names this entity is known by (e.g. "M. Chen", an email address,
-- a former company name). Checked alongside `name` during brainops lookup
-- (step 4) so near-duplicate mentions resolve to the same entity instead of
-- spawning a new row. Populated manually for now via brainops.add_alias();
-- LLM-suggested aliases are a later upgrade.
ALTER TABLE entities ADD COLUMN IF NOT EXISTS aliases TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS entities_aliases_idx ON entities USING gin (aliases);

-- pages: one per entity, mirrors .md source of truth (ingest step 5-7)
CREATE TABLE IF NOT EXISTS pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID REFERENCES entities(id) NOT NULL,
    file_path TEXT UNIQUE NOT NULL,
    content_hash TEXT NOT NULL,
    compiled_truth TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- facts: atomic fact rows per page
CREATE TABLE IF NOT EXISTS facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id UUID REFERENCES pages(id) ON DELETE CASCADE,
    fact TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- provenance: which raw source doc (see git_commit.save_raw_sidecar's
-- wiki/raw/... path) this fact was extracted from. Only set going forward --
-- NULL for anything ingested before this column existed.
ALTER TABLE facts ADD COLUMN IF NOT EXISTS source_doc TEXT;

-- facts are append-only across re-ingests (postgres_sync.py inserts the full
-- current set every run instead of delete+reinsert). This unique index is
-- what makes that safe: a fact already on record hits ON CONFLICT DO NOTHING
-- and keeps its original source_doc rather than being duplicated or
-- silently reattributed to whichever doc happened to repeat it.
CREATE UNIQUE INDEX IF NOT EXISTS facts_page_fact_uidx
    ON facts (page_id, lower(fact));

-- timeline_entries
CREATE TABLE IF NOT EXISTS timeline_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id UUID REFERENCES pages(id) ON DELETE CASCADE,
    event_date TEXT,
    event TEXT NOT NULL
);

ALTER TABLE timeline_entries ADD COLUMN IF NOT EXISTS source_doc TEXT;

-- same append-only pattern as facts_page_fact_uidx. COALESCE on event_date
-- because Postgres treats every NULL as distinct in a unique index by
-- default -- without it, two undated entries with identical text wouldn't
-- collide and would duplicate on every re-ingest.
CREATE UNIQUE INDEX IF NOT EXISTS timeline_entries_page_event_uidx
    ON timeline_entries (page_id, COALESCE(event_date, ''), lower(event));

-- events: append-only ledger of every ingest run that *touches* an entity,
-- independent of whether it produced any new facts or timeline entries.
-- This is the piece timeline_entries can't cover: timeline_entries is a
-- deduped, curated view of dated happenings *in the world* (what the doc
-- describes), while events is a raw log of *our* enrichment activity (when
-- we looked at this entity, and whether anything new came of it).
--
-- Fires exactly once per ingest run per entity, from postgres_sync.sync()
-- (step 7) -- unconditionally, even in the case that used to be silently
-- dropped: content_changed=false and zero new facts/timeline rows written
-- (a doc that only reconfirms what's already known). Without this row,
-- "never touched" and "touched, nothing new" were indistinguishable, and
-- a staleness sweep ("hasn't been enriched in 90+ days") had nothing to
-- query against.
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID REFERENCES entities(id) ON DELETE CASCADE NOT NULL,
    page_id UUID REFERENCES pages(id) ON DELETE SET NULL,
    -- constrained TEXT, same re-runnable pattern as entity_type/link_type --
    -- 'ingest' is the only source today; room for 'manual_edit', 'lint_flag',
    -- etc. later without a type migration.
    event_type TEXT NOT NULL DEFAULT 'ingest'
        CHECK (event_type IN ('ingest')),
    -- raw sidecar path -- same value written to facts/timeline_entries/
    -- content_chunks.source_doc for this same ingest run, so an event row
    -- can always be joined back to what it produced elsewhere.
    source_doc TEXT,
    content_changed BOOLEAN NOT NULL,
    facts_written INT NOT NULL DEFAULT 0,
    timeline_written INT NOT NULL DEFAULT 0,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- staleness/enrichment-sweep query shape: MAX(occurred_at) WHERE entity_id = ...
CREATE INDEX IF NOT EXISTS events_entity_occurred_idx
    ON events (entity_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS events_source_doc_idx
    ON events (source_doc) WHERE source_doc IS NOT NULL;

-- content_chunks: vector + keyword search (retrieval steps 3-4)
CREATE TABLE IF NOT EXISTS content_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id UUID REFERENCES pages(id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    embedding vector(1536),           -- text-embedding-3-small
    tsv tsvector,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- chunk_type separates chunks that are safe to fully rebuild every sync
-- (compiled_truth/fact/timeline -- derived from data that already lives
-- canonically in pages/facts/timeline_entries) from 'raw' chunks, which are
-- the only copy of anything the LLM extraction dropped or paraphrased and
-- must never be deleted on re-sync. See postgres_sync.py.
ALTER TABLE content_chunks ADD COLUMN IF NOT EXISTS chunk_type TEXT
    NOT NULL DEFAULT 'compiled_truth'
    CHECK (chunk_type IN ('compiled_truth', 'fact', 'timeline', 'raw'));

-- set only on chunk_type='raw' rows; same raw sidecar path as facts/timeline
-- source_doc above. Doubles as the idempotency key so re-running sync() for
-- an unchanged raw doc doesn't re-embed/duplicate it (see postgres_sync.py).
ALTER TABLE content_chunks ADD COLUMN IF NOT EXISTS source_doc TEXT;

CREATE INDEX IF NOT EXISTS content_chunks_embedding_idx
    ON content_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS content_chunks_tsv_idx
    ON content_chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS content_chunks_page_type_idx
    ON content_chunks (page_id, chunk_type);

CREATE INDEX IF NOT EXISTS content_chunks_source_doc_idx
    ON content_chunks (source_doc) WHERE source_doc IS NOT NULL;

CREATE OR REPLACE FUNCTION content_chunks_tsv_trigger() RETURNS trigger AS $$
BEGIN
  NEW.tsv := to_tsvector('english', NEW.chunk_text);
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tsvectorupdate ON content_chunks;
CREATE TRIGGER tsvectorupdate BEFORE INSERT OR UPDATE
ON content_chunks FOR EACH ROW EXECUTE FUNCTION content_chunks_tsv_trigger();

-- links: typed edges for graph walk (ingest step 8, retrieval step 2)
CREATE TABLE IF NOT EXISTS links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id UUID REFERENCES entities(id) ON DELETE CASCADE,
    to_entity_id UUID REFERENCES entities(id) ON DELETE CASCADE,
    link_type TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- fixed enum of relationship kinds, same constrained-TEXT pattern as
-- entity_type above (re-runnable: DROP then ADD so setup_db.py can apply
-- init.sql repeatedly while the vocabulary is still being tuned).
ALTER TABLE links DROP CONSTRAINT IF EXISTS links_link_type_check;
ALTER TABLE links ADD CONSTRAINT links_link_type_check
    CHECK (link_type IN (
        'works_at', 'founded', 'created', 'invested_in',
        'acquired', 'partnered_with', 'located_in',
        'part_of', 'attended', 'related_to'
    ));

-- explicit/inferred, same as facts (ingest step 3/8)
ALTER TABLE links ADD COLUMN IF NOT EXISTS confidence TEXT
    CHECK (confidence IN ('explicit', 'inferred'));

-- short quote/paraphrase grounding why this edge was extracted (ingest step 8)
ALTER TABLE links ADD COLUMN IF NOT EXISTS evidence TEXT;

CREATE INDEX IF NOT EXISTS links_from_idx ON links (from_entity_id, link_type);
CREATE INDEX IF NOT EXISTS links_to_idx ON links (to_entity_id, link_type);