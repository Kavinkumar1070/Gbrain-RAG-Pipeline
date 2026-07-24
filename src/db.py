"""All database access. Every other module talks to Postgres through this file only."""
import json
import re
import unicodedata
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

from src import config


@contextmanager
def get_conn():
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
    register_vector(conn)
    try:
        yield conn
    finally:
        conn.close()


def init_schema():
    """Run once to create tables. Reads db/schema.sql, substituting the
    {{ENTITY_TYPE_CHECK}} placeholder with the current ENTITY_TYPES env var
    (config.ENTITY_TYPES) so the entities.type CHECK constraint always
    matches whatever types .env declares -- this is what makes adding a new
    entity type a .env-only change instead of a schema.sql edit."""
    with open("db/schema.sql") as f:
        sql = f.read()
    type_list_sql = ", ".join(f"'{t}'" for t in config.ENTITY_TYPES)
    sql = sql.replace("{{ENTITY_TYPE_CHECK}}", type_list_sql)
    with get_conn() as conn:
        conn.execute(sql)


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\s-]", "", name).strip().lower()
    return re.sub(r"[\s_]+", "-", name)


# ---------- sources (file-level ingest dedup) ----------

def get_source_by_hash(content_hash: str) -> dict | None:
    """Looks up a prior ingest by content hash. A hit means this exact file
    content -- regardless of path/filename -- was already processed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, content_hash, mime_type, filepath, ingested_at FROM sources WHERE content_hash = %s",
            (content_hash,),
        ).fetchone()
    if not row:
        return None
    cols = ["id", "content_hash", "mime_type", "filepath", "ingested_at"]
    return dict(zip(cols, row))


def record_source(content_hash: str, mime_type: str, filepath: str):
    """Marks this content hash as ingested. Called once, after a run
    completes successfully -- a failed run should be retryable, not
    permanently marked as already-seen."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sources (content_hash, mime_type, filepath)
            VALUES (%s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            """,
            (content_hash, mime_type, filepath),
        )


# ---------- entities ----------

def get_all_entities(entity_type: str | None = None) -> list[dict]:
    """Used by dedupe.py for fuzzy/embedding matching. Small POC scale: load all into memory."""
    q = "SELECT id, canonical_name, type, slug, aliases, tier, name_embedding FROM entities"
    params = ()
    if entity_type:
        q += " WHERE type = %s"
        params = (entity_type,)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    cols = ["id", "canonical_name", "type", "slug", "aliases", "tier", "name_embedding"]
    return [dict(zip(cols, r)) for r in rows]


def create_entity(name: str, entity_type: str, embedding: list[float]) -> str:
    base_slug = slugify(name)
    slug = base_slug
    with get_conn() as conn:
        # handle slug collisions (e.g. two "David Liu"s)
        n = 1
        while conn.execute("SELECT 1 FROM entities WHERE slug = %s", (slug,)).fetchone():
            n += 1
            slug = f"{base_slug}-{n}"

        row = conn.execute(
            """
            INSERT INTO entities (canonical_name, type, slug, aliases, name_embedding)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, entity_type, slug, json.dumps([]), embedding),
        ).fetchone()
    return str(row[0])


def add_alias(entity_id: str, alias: str):
    with get_conn() as conn:
        row = conn.execute("SELECT aliases FROM entities WHERE id = %s", (entity_id,)).fetchone()
        aliases = row[0] or []
        if alias not in aliases:
            aliases.append(alias)
            conn.execute(
                "UPDATE entities SET aliases = %s, updated_at = now() WHERE id = %s",
                (json.dumps(aliases), entity_id),
            )


def get_entity(entity_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, canonical_name, type, slug, aliases, tier FROM entities WHERE id = %s",
            (entity_id,),
        ).fetchone()
    if not row:
        return None
    cols = ["id", "canonical_name", "type", "slug", "aliases", "tier"]
    return dict(zip(cols, row))


def set_tier(entity_id: str, tier: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE entities SET tier = %s, updated_at = now() WHERE id = %s AND tier > %s",
            (tier, entity_id, tier),  # only ever escalate (lower number = higher tier), never downgrade
        )


# ---------- events (timeline) ----------

def insert_event(entity_id: str, source_type: str, source_ref: str, content: str, confidence: str = "observed"):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO events (entity_id, source_type, source_ref, content, confidence)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (entity_id, source_type, source_ref, content, confidence),
        )


def get_events(entity_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT source_type, source_ref, content, confidence, observed_at
            FROM events WHERE entity_id = %s
            ORDER BY observed_at DESC
            """,
            (entity_id,),
        ).fetchall()
    cols = ["source_type", "source_ref", "content", "confidence", "observed_at"]
    return [dict(zip(cols, r)) for r in rows]


def count_events(entity_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT count(*) FROM events WHERE entity_id = %s", (entity_id,)).fetchone()
    return row[0]


# ---------- facts (compiled truth) ----------

def insert_fact(entity_id: str, field: str, value: str, source: str, confidence: str = "observed"):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO facts (entity_id, field, value, source, confidence)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (entity_id, field, value, source, confidence),
        )


def get_latest_facts(entity_id: str) -> dict:
    """Returns {field: {value, source, confidence, observed_at}} using latest observed_at per field.
    Contradictions (multiple sources disagreeing) are visible via get_all_facts()."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (field) field, value, source, confidence, observed_at
            FROM facts WHERE entity_id = %s
            ORDER BY field, observed_at DESC
            """,
            (entity_id,),
        ).fetchall()
    return {r[0]: {"value": r[1], "source": r[2], "confidence": r[3], "observed_at": r[4]} for r in rows}


def get_all_facts(entity_id: str) -> list[dict]:
    """All facts, including superseded/contradicting ones -- for surfacing contradictions in lint."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT field, value, source, confidence, observed_at
            FROM facts WHERE entity_id = %s
            ORDER BY field, observed_at DESC
            """,
            (entity_id,),
        ).fetchall()
    cols = ["field", "value", "source", "confidence", "observed_at"]
    return [dict(zip(cols, r)) for r in rows]


# ---------- relationships (graph) ----------

def insert_relationship(from_id: str, to_id: str, relation_type: str, source_ref: str = None) -> bool:
    """Insert an edge, or no-op if this exact (from, to, relation_type) edge
    already exists (see UNIQUE constraint in schema.sql) -- this is what
    stops the same relationship line from being duplicated on a wiki page
    when a source is re-ingested or a second source restates the same edge.
    Returns True if a new row was inserted, False if it was already there,
    so the caller can log which happened."""
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO relationships (from_entity_id, to_entity_id, relation_type, source_ref)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO NOTHING
            RETURNING id
            """,
            (from_id, to_id, relation_type, source_ref),
        ).fetchone()
    return row is not None


def get_relationships(entity_id: str) -> list[dict]:
    """All relationships touching this entity -- outbound (this entity is
    from_entity_id) AND inbound (this entity is to_entity_id).

    Each relationship is written to the DB exactly once, from whichever side
    the extraction skill happened to state it. Previously this only queried
    the outbound direction, so an edge only showed up on the page of the
    entity it was written *from* -- e.g. the meeting page showed
    "attended -> Sarah" but Sarah's own page showed no relationship back to
    the meeting at all, unless the LLM happened to also extract the mirror
    fact from Sarah's side independently (unreliable, and not how gbrain's
    graph layer is supposed to work: "if Entity A links to Entity B, B's
    page shows the backlink too", computed from stored edges, not re-stated
    per entity at ingest time).

    `direction` tells the renderer which way the arrow points so inbound
    edges don't get mislabeled as this entity's own action.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.relation_type, e.canonical_name, e.slug, e.type, r.source_ref, 'out' AS direction
            FROM relationships r
            JOIN entities e ON e.id = r.to_entity_id
            WHERE r.from_entity_id = %s

            UNION ALL

            SELECT r.relation_type, e.canonical_name, e.slug, e.type, r.source_ref, 'in' AS direction
            FROM relationships r
            JOIN entities e ON e.id = r.from_entity_id
            WHERE r.to_entity_id = %s
            """,
            (entity_id, entity_id),
        ).fetchall()
    cols = ["relation_type", "name", "slug", "type", "source_ref", "direction"]
    return [dict(zip(cols, r)) for r in rows]


def graph_query(cypher_like_hops: int = 2):
    """Example: 'who do I know who works at companies I have relationships with'.
    This is the query shape markdown/grep cannot answer -- multi-hop join."""
    sql = """
        SELECT p1.canonical_name AS person, c.canonical_name AS company, r2.relation_type
        FROM entities p1
        JOIN relationships r1 ON r1.from_entity_id = p1.id AND r1.relation_type = 'knows'
        JOIN entities p2 ON p2.id = r1.to_entity_id
        JOIN relationships r2 ON r2.from_entity_id = p2.id AND r2.relation_type = 'works_at'
        JOIN entities c ON c.id = r2.to_entity_id
        WHERE p1.type = 'person'
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return rows


# ---------- chunks (retrieval) ----------

def replace_chunks(source_ref: str, chunks: list[str], embeddings: list[list[float]], source_type: str = "source"):
    """Deletes any existing chunks for this source_ref, then inserts the new
    set. Re-ingesting the same file should replace its chunks, not
    accumulate duplicates alongside the old ones -- source files are
    immutable in principle, but a POC will get re-run on the same path
    plenty of times while iterating.

    `source_type` distinguishes raw ingested text ('source', e.g. sources/*.txt)
    from a rendered wiki page ('wiki_page', e.g. wiki/people/sarah-chen.md) --
    same table, same two representations (embedding + tsvector, the latter
    computed automatically by the generated `chunk_tsv` column), just a
    different origin so a caller can filter to one or the other at query time.
    """
    assert len(chunks) == len(embeddings), "chunks and embeddings must be same length"
    with get_conn() as conn:
        conn.execute("DELETE FROM chunks WHERE source_ref = %s", (source_ref,))
        for i, (text, emb) in enumerate(zip(chunks, embeddings)):
            conn.execute(
                """
                INSERT INTO chunks (source_ref, source_type, chunk_index, chunk_text, chunk_embedding)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (source_ref, source_type, i, text, emb),
            )


def search_chunks(query_embedding: list[float], top_k: int = 5, source_type: str | None = None) -> list[dict]:
    """Cosine-distance nearest-neighbor search over chunks (pgvector `<=>`
    operator = cosine distance, so ORDER BY ascending = most similar first).
    Returns source_ref alongside each chunk so the caller can cross-reference
    which entities were touched by that source (via events.source_ref) --
    chunks intentionally don't carry an entity_id themselves; see schema.sql.
    Pass source_type='source' or 'wiki_page' to search only one namespace."""
    q = """
        SELECT chunk_text, source_ref, source_type, chunk_index, chunk_embedding <=> %s AS distance
        FROM chunks
    """
    params = [query_embedding]
    if source_type:
        q += " WHERE source_type = %s"
        params.append(source_type)
    q += " ORDER BY chunk_embedding <=> %s LIMIT %s"
    params += [query_embedding, top_k]
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    cols = ["chunk_text", "source_ref", "source_type", "chunk_index", "distance"]
    return [dict(zip(cols, r)) for r in rows]


def search_chunks_keyword(query: str, top_k: int = 5, source_type: str | None = None) -> list[dict]:
    """Keyword search via Postgres full-text search (chunk_tsv, GIN-indexed).
    `plainto_tsquery` handles arbitrary user text (no operator syntax needed).
    ts_rank descending = most relevant first."""
    q = """
        SELECT chunk_text, source_ref, source_type, chunk_index,
               ts_rank(chunk_tsv, plainto_tsquery('english', %s)) AS rank
        FROM chunks
        WHERE chunk_tsv @@ plainto_tsquery('english', %s)
    """
    params = [query, query]
    if source_type:
        q += " AND source_type = %s"
        params.append(source_type)
    q += " ORDER BY rank DESC LIMIT %s"
    params.append(top_k)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    cols = ["chunk_text", "source_ref", "source_type", "chunk_index", "rank"]
    return [dict(zip(cols, r)) for r in rows]


def search_chunks_hybrid(
    query: str, query_embedding: list[float], top_k: int = 5, source_type: str | None = None, k: int = 60
) -> list[dict]:
    """Hybrid retrieval: runs vector search and keyword search separately,
    then merges with Reciprocal Rank Fusion (RRF) -- score = sum of
    1 / (k + rank_in_each_list). RRF is used instead of e.g. averaging raw
    cosine distance and ts_rank because those two scores live on unrelated
    scales; RRF only needs each list's *rank order*, not comparable scores.
    `k` is RRF's standard damping constant (60 is the commonly used default)."""
    vector_hits = search_chunks(query_embedding, top_k=top_k * 2, source_type=source_type)
    keyword_hits = search_chunks_keyword(query, top_k=top_k * 2, source_type=source_type)

    scores: dict[tuple[str, int], float] = {}
    info: dict[tuple[str, int], dict] = {}
    for rank, hit in enumerate(vector_hits):
        key = (hit["source_ref"], hit["chunk_index"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        info[key] = hit
    for rank, hit in enumerate(keyword_hits):
        key = (hit["source_ref"], hit["chunk_index"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        info.setdefault(key, hit)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [{**info[key], "rrf_score": score} for key, score in ranked]