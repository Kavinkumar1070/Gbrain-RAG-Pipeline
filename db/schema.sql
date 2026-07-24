-- gbrain-poc schema
-- Four primitives: entities (registry), events (ledger), facts (store), relationships (graph)

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- Drop-and-recreate: CREATE TABLE IF NOT EXISTS below is a no-op against a
-- table that already exists, even if its column set is stale (e.g. an old
-- `chunks` table from before source_type/chunk_tsv were added) -- setup_db.py
-- would silently keep the old shape instead of picking up schema changes.
-- Dropping first makes re-running this file always produce the current
-- schema. DESTRUCTIVE -- this wipes all existing data. Comment this block
-- out if you need to preserve data across a schema change instead (and
-- hand-write an ALTER TABLE migration for the new columns/constraints).
-- Child tables first (relationships/events/facts/chunks all reference
-- entities), though CASCADE would handle either order.
DROP TABLE IF EXISTS chunks CASCADE;
DROP TABLE IF EXISTS relationships CASCADE;
DROP TABLE IF EXISTS facts CASCADE;
DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS entities CASCADE;
DROP TABLE IF EXISTS sources CASCADE;

-- 0. Source registry: one row per distinct file *content* ever ingested.
-- content_hash (sha256 of raw bytes) is the actual dedupe key -- it catches
-- the same file re-run under the same path, the same file copied/renamed to
-- a new path, and a byte-identical re-download, all as the same case.
-- filepath/mime_type are kept for display only and are NOT part of the
-- identity check (two different paths with the same hash are still the same
-- source and must not re-ingest). Re-running a path whose content has since
-- changed is NOT caught here on purpose (different hash = legitimately new
-- content, e.g. an updated transcript) -- that's a fresh ingest, not a dupe.
CREATE TABLE IF NOT EXISTS sources (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_hash   TEXT NOT NULL UNIQUE,   -- sha256 hex digest of raw file bytes
    mime_type      TEXT NOT NULL,          -- e.g. 'application/pdf', 'text/plain'
    filepath       TEXT NOT NULL,          -- path at first-ingest time, for display/debugging only
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 1. Entity registry: canonical identity + aliases + dedupe embedding
CREATE TABLE IF NOT EXISTS entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT NOT NULL,
    -- {{ENTITY_TYPE_CHECK}} is substituted by src/db.py:init_schema() from
    -- config.ENTITY_TYPES (i.e. the ENTITY_TYPES env var) so the DB-level
    -- constraint always matches whatever types .env currently declares --
    -- no manual ALTER TABLE needed when you add/remove a type in .env,
    -- just re-run `python setup_db.py`.
    type            TEXT NOT NULL CHECK (type IN ({{ENTITY_TYPE_CHECK}})),
    slug            TEXT UNIQUE NOT NULL,
    aliases         JSONB NOT NULL DEFAULT '[]',
    tier            INT NOT NULL DEFAULT 3 CHECK (tier IN (1, 2, 3)),
    name_embedding  VECTOR(1536),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Event ledger: immutable, append-only signal log (-> Timeline section)
CREATE TABLE IF NOT EXISTS events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id    UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source_type  TEXT NOT NULL,       -- 'pdf' | 'transcript' | 'txt' | 'manual_doc'
    source_ref   TEXT,                -- path to raw source file
    content      TEXT NOT NULL,       -- what happened, in plain text
    confidence   TEXT NOT NULL DEFAULT 'observed'
                 CHECK (confidence IN ('observed', 'self-described', 'inferred')),
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Fact store: structured claims with provenance (-> Compiled Truth section)
CREATE TABLE IF NOT EXISTS facts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id    UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    field        TEXT NOT NULL,       -- 'role' | 'company' | 'belief' | 'relationship' | ...
    value        TEXT NOT NULL,
    source       TEXT NOT NULL,       -- 'manual_doc' | 'transcript:<path>' | ...
    confidence   TEXT NOT NULL DEFAULT 'observed'
                 CHECK (confidence IN ('observed', 'self-described', 'inferred')),
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. Relationship graph: typed edges between entities
-- UNIQUE(from_entity_id, to_entity_id, relation_type) -- an edge is an
-- identity, not an event: the same "A works_at B" stated again (re-ingest of
-- the same source, or a second source restating a fact already on file)
-- must not create a second row. This is what previously duplicated
-- "## Relationships" lines on wiki .md pages -- get_relationships() returned
-- N identical rows for the same edge and render_md.py rendered N lines.
-- If the edge direction is later reversed or the type changes, that's a
-- different (from, to, type) tuple and is correctly treated as a new edge.
CREATE TABLE IF NOT EXISTS relationships (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id  UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_entity_id    UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,    -- 'works_at' | 'co_founded' | 'invested_in' | 'knows' | ...
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    source_ref      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_entity_id, to_entity_id, relation_type)
);

-- 5. Chunk store: raw-source text chunks + embeddings, for semantic retrieval
-- ("what do we know about X" queries -- gbrain's query/RAG layer). This is
-- deliberately decoupled from entities: a chunk isn't "owned" by one entity
-- (a transcript paragraph usually mentions several), so entity association
-- is recovered at query time via source_ref -> events.source_ref, not stored
-- here as a foreign key. name_embedding on entities is a SEPARATE, narrower
-- embedding used only for dedupe matching on short name strings -- do not
-- reuse chunk_embedding for that, and do not reuse name_embedding for this.
CREATE TABLE IF NOT EXISTS chunks (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_ref       TEXT NOT NULL,       -- path to the file this chunk came from (raw source OR rendered wiki page)
    source_type      TEXT NOT NULL DEFAULT 'source'
                     CHECK (source_type IN ('source', 'wiki_page')),  -- 'source' = raw sources/*.txt,
                     -- 'wiki_page' = the rendered, compiled wiki/*.md page (post-dedupe/compose-page truth,
                     -- not the raw transcript) -- kept as a separate namespace from 'source' chunks for the
                     -- same underlying entity, since they answer different questions at query time
                     -- ("what was said" vs "what do we currently believe").
    chunk_index      INT NOT NULL,        -- order within the source file, 0-based
    chunk_text       TEXT NOT NULL,
    chunk_embedding  VECTOR(1536) NOT NULL,
    chunk_tsv        TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_ref, chunk_index)      -- re-ingesting the same file replaces, not duplicates
);

-- Indexes
-- (sources.content_hash and relationships' (from,to,relation_type) already
-- have indexes via their UNIQUE constraints above -- no separate CREATE INDEX needed)
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_slug ON entities(slug);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_facts_entity_field ON facts(entity_id, field);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_entity_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_ref);
CREATE INDEX IF NOT EXISTS idx_chunks_source_type ON chunks(source_type);

-- Keyword-search index (Postgres full-text search over chunk_tsv)
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (chunk_tsv);

-- Vector index for embedding-based dedupe fallback (requires pgvector >= 0.5 for ivfflat with cosine)
CREATE INDEX IF NOT EXISTS idx_entities_embedding
    ON entities USING ivfflat (name_embedding vector_cosine_ops)
    WITH (lists = 100);

-- Vector index for chunk retrieval (semantic search over ingested source text)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING ivfflat (chunk_embedding vector_cosine_ops)
    WITH (lists = 100);