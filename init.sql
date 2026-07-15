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

-- timeline_entries
CREATE TABLE IF NOT EXISTS timeline_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id UUID REFERENCES pages(id) ON DELETE CASCADE,
    event_date TEXT,
    event TEXT NOT NULL
);

-- content_chunks: vector + keyword search (retrieval steps 3-4)
CREATE TABLE IF NOT EXISTS content_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id UUID REFERENCES pages(id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    embedding vector(1536),           -- text-embedding-3-small
    tsv tsvector,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS content_chunks_embedding_idx
    ON content_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS content_chunks_tsv_idx
    ON content_chunks USING gin (tsv);

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

CREATE INDEX IF NOT EXISTS links_from_idx ON links (from_entity_id, link_type);
CREATE INDEX IF NOT EXISTS links_to_idx ON links (to_entity_id, link_type);