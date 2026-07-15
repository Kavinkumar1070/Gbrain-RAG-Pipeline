# GBrain

Personal knowledge graph + RAG system. Ingests documents into a git-backed wiki (`.md` = source of truth), syncs to Postgres for hybrid retrieval (vector + keyword + graph), and answers queries with cited synthesis.

## Architecture

Two flows, each a linear pipeline (no agent framework — fixed steps, LLM used only at specific points).

### Ingest flow
```
1. File arrives (pdf / docx / txt)
2. Extract raw text
3. LLM pass (skill) -> entity + facts + take + timeline + wikilinks
4. Brain-ops lookup: entity exists?
     yes -> pull compiled_truth as merge context
     no  -> fresh page, no prior context
5. Page write -> compiled_truth + facts rows + timeline_entries + frontmatter
6. Commit to git (.md file = source of truth)
7. Sync to Postgres (parse .md -> pages row w/ content_hash check -> chunk + embed -> content_chunks -> tsvector auto-trigger)
8. Graph edge extraction (zero-LLM regex over wikilinks -> links table, typed edges)
```

### Retrieval flow
```
1. Query arrives (agent or user)
2. Query expansion (optional, 1 LLM call)
3. Parallel search: vector (HNSW cosine) + keyword (tsvector/ts_rank) + graph walk (links table)
4. Reciprocal Rank Fusion: score = sum of 1 / (60 + rank)
5. Rerank (optional, cross-encoder / ZeroEntropy)
6. Fetch full page context (compiled_truth + timeline_entries + facts)
7. Synthesis (LLM call) -> cited answer, explicit gap notes
8. Return to agent (answer + citations via MCP / API)
```

## Stack

- **LLM / embeddings**: Azure OpenAI (`gpt-5.4` chat, `text-embedding-3-small`)
- **DB**: Supabase Postgres + pgvector (HNSW index) + native `tsvector`/`ts_rank`
- **Source of truth**: `.md` files committed to git (`wiki/`)
- **Language**: Python, no agent framework — plain functions / LangGraph if state persistence is needed later

## Setup

```bash
cp .env.example .env          # fill in Azure + Supabase credentials
pip install -r requirements.txt --break-system-packages
python setup_db.py            # applies schema, verifies tables + extensions
```

## Project structure

```
gbrain/
  init.sql              # Postgres schema (entities, pages, facts, timeline_entries, content_chunks, links)
  setup_db.py            # applies schema + verifies (no psql CLI needed)
  setup_db.sh             # same, via psql CLI
  requirements.txt
  .env.example
  wiki/                   # .md source-of-truth pages, git-tracked
  pipeline/
    steps/
      extract.py          # steps 1-2: file arrives -> raw text (pdf/docx/txt)
      llm_pass.py          # step 3: entity/facts/take/timeline/wikilinks extraction
      brainops.py           # step 4: entity lookup
      page_write.py          # step 5: page composition
      git_sync.py             # step 6: commit to git
      pg_sync.py                # step 7: parse + chunk + embed + sync to Postgres
      graph_extract.py           # step 8: wikilink -> typed edges
    skills/
      ingest_extract/
        SKILL.md            # instructions for step 3 LLM pass
    ingest.py               # orchestrates steps 1-8
    retrieval.py              # orchestrates retrieval steps 1-8
```

## Status

- [x] Postgres schema + Supabase setup
- [x] Steps 1-2: file extraction (pdf/docx/txt)
- [ ] Step 3: LLM extraction pass
- [ ] Step 4-8: brain-ops, page write, git sync, pg sync, graph extraction
- [ ] Retrieval flow
