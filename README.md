# gbrain-poc

A DB-backed replica of the [gbrain](https://github.com/garrytan/gbrain) architecture.
Built on Azure OpenAI + Postgres/pgvector. No agent framework -- a "skill" here
is just a markdown file used as an LLM's system prompt for one structured call;
everything else is deterministic Python.

## How it works

```
source file (.txt / .md / .pdf)
        |
   signal_detector.py         -- deterministic: classify transcript | pdf | txt
        |
   source_reader.py           -- deterministic: pull raw text out of the file
        |
   [LLM call 1] skills/RESOLVER.md        -- "which skill handles this?" -> JSON {"skill": ...}
        |
   [LLM call 2] skills/<skill>/SKILL.md   -- extract entities/facts/relationships -> JSON
        |
   ====================== everything below this line is plain Python ======================
        |
   for each entity extracted:
        dedupe.find_entity()            -- fuzzy + embedding match against the DB
        db.create_entity() / add_alias() -- new -> create; matched-via-alias -> record it
        manual_docs.lookup_manual_doc()  -- new entity only: check enrichment_docs/ for a
                                             human-written profile (one small internal LLM
                                             call to structure that doc into fields)
        db.insert_fact() / insert_event() -- write the extracted fact + timeline entry
        db.insert_relationship()         -- for each `related` entry, same dedupe/create path
        db.set_tier()                    -- escalate based on db.count_events(), never downgrade
        |
   render_md.render_entity_page() for every touched entity
        |
        [LLM call 3] skills/compose-page/SKILL.md -- write the page's prose summary
                       from that entity's current facts/events (falls back to a
                       deterministic one-liner if this call fails)
        |
        writes wiki/<people|companies>/<slug>.md
        |
   git_commit.commit_wiki() -- one commit for the whole run
```

**Exactly three LLM calls happen per ingest run** (routing, extraction, and one
compose-page call per touched entity) -- each is a single request/response, no
tools, no loop, no filesystem access for the model. The model can only return
text; Python decides what happens with it. This is intentional: dedupe, DB
writes, tiering, file writes, and git are all rule-based, so there's no reason
to let a model improvise them -- and no way for a "successful write" to be
fabricated, because the model was never given a write tool to fabricate with.

## Project layout

```
gbrain-poc/
├── skills/                       -- markdown files used as LLM system prompts
│   ├── RESOLVER.md                -- routing: transcript vs everything else (JSON out)
│   ├── meeting-ingestion/SKILL.md -- transcript extraction: person/company/meeting/
│   │                                  project/deal (JSON out)
│   ├── media-ingest/SKILL.md      -- generic extraction for PDFs/docs/notes/email:
│   │                                  person/company/project/deal (JSON out)
│   └── compose-page/SKILL.md      -- page prose composition (JSON out)
├── src/
│   ├── config.py              -- env var loading
│   ├── azure_client.py        -- Azure OpenAI chat + embedding calls (the only place
│   │                              that talks to the model)
│   ├── skill_runner.py        -- run_skill(): read a SKILL.md, one structured chat call,
│   │                              parse JSON. This is the entire "agent."
│   ├── db.py                  -- all SQL: entities / events / facts / relationships
│   ├── dedupe.py              -- fuzzy + embedding identity match (plain Python)
│   ├── resolver.py            -- entity type -> brain folder (plain Python)
│   ├── manual_docs.py         -- reads enrichment_docs/ as a stand-in for paid APIs
│   ├── render_md.py           -- DB record -> markdown page (calls compose-page skill
│   │                              for the summary, writes the file itself)
│   └── git_commit.py          -- commits wiki/ changes
├── db/schema.sql               -- the four DB primitives
├── setup_db.py                  -- one-time schema creation
├── run_pipeline.py               -- entrypoint
├── enrichment_docs/               -- your manually-written profile notes ("API" stand-in)
└── sources/                        -- drop input files here
```

## The four DB primitives (`db/schema.sql`)

| Table | Role | Maps to markdown |
|---|---|---|
| `entities` | canonical identity + aliases + embedding (type: person/company/meeting/project/deal) | filename / page identity |
| `events` | immutable append-only signal log | Timeline section |
| `facts` | structured claims with provenance/confidence | Compiled Truth section |
| `relationships` | typed graph edges | Relationships section + graph queries |

`db.graph_query()` shows the kind of multi-hop query markdown/grep can't do:
"who do I know who works at a company I have a relationship with." This is
actually populated now -- `run_pipeline.py` calls `insert_relationship` for
every `related` entry a skill extracts.

## Retrieval: the `chunks` table

A fifth table, deliberately separate from the four primitives above, backs
free-text retrieval ("what do we know about X" style queries -- gbrain's
query/RAG layer). Every ingest run populates it twice per file:

- **`source_type='source'`** -- chunks of the raw input file (`sources/*.txt`),
  i.e. what was literally said.
- **`source_type='wiki_page'`** -- chunks of the *rendered* `wiki/*.md` page
  after dedupe + compose-page, i.e. the compiled, current truth for that
  entity. This is what makes the deduped/compiled knowledge retrievable, not
  just the original transcript. Per gbrain's principle that the committed
  `.md` file -- not the DB -- is the ultimate source of truth, every page now
  also renders a `## Fact History` section (`db.get_all_facts()`, not just
  `get_latest_facts()`): every fact ever inserted, grouped by field, newest
  first, with superseded/contradicting values kept and labeled rather than
  silently overwritten. If the DB were lost, the `.md` files alone (plus the
  full `## Timeline`, which was already complete) are enough to reconstruct
  `events` and `facts` -- only `entities.name_embedding` and
  `chunks.chunk_embedding` can't be recovered from markdown, since embeddings
  aren't rendered anywhere.

Each chunk carries two independent representations, kept in sync
automatically:

- `chunk_embedding` (`VECTOR(1536)`) -- semantic/nearest-neighbor search via
  pgvector's `<=>` cosine-distance operator (`db.search_chunks`).
- `chunk_tsv` (`TSVECTOR`, a `GENERATED ALWAYS AS` column, GIN-indexed) --
  exact keyword search via Postgres full-text search (`db.search_chunks_keyword`).

`db.search_chunks_hybrid()` merges both with Reciprocal Rank Fusion (RRF),
since cosine distance and `ts_rank` aren't on comparable scales -- RRF only
needs each list's rank order. Chunks intentionally don't carry an `entity_id`
(a transcript paragraph or wiki page usually mentions several entities);
cross-referencing back to an entity happens at query time via `source_ref`.

Population is ingestion-time only -- no query/search happens inside
`run_pipeline.py` itself; that's a separate, later piece.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # fill in your Azure + DB credentials
python setup_db.py              # creates pgvector extension + 4 tables
```

Requires Postgres with the `pgvector` extension available.

## Run it

Two sample files are included:

- `enrichment_docs/sarah-chen.txt` — a manually-written profile (your "API" stand-in)
- `sources/2026-07-23-product-review.txt` — a fake meeting transcript mentioning Sarah,
  Mike, and two brand-new entities (Alex Rivera, Meridian Labs)

```bash
python run_pipeline.py sources/2026-07-23-product-review.txt
```

What should happen -- watch the console output, every enrich decision is printed:
- `[route]` -> `meeting-ingestion` (LLM call 1).
- `[extract]` -> the meeting itself plus Sarah, Mike, Alex Rivera, Meridian Labs, with
  relationships where stated (LLM call 2).
- Per entity: `[new]`/`[match]` (dedupe result), `[enrich]` (manual-doc hit or not --
  Sarah matches `enrichment_docs/sarah-chen.txt`), `[event]` (timeline write),
  `[relate]` (graph edges), `[tier]` (escalation or not). All plain Python, no LLM.
- `[render]` -- each touched entity's page (LLM call 3 per entity, summary only).
- One git commit for the whole run.
- Check `wiki/people/` and `wiki/companies/` for the generated `.md` pages, and
  `git -C wiki log` for the commit.

Run it again with a transcript that mentions Sarah a second time and her page should
**update** (new timeline entry, possible tier escalation) instead of duplicating --
that confirms the dedupe path is working as intended.

## Extending this POC

- **New entity types**: already at 5 -- person, company, meeting, project, deal.
  To add another, extend `FOLDER_MAP` in `resolver.py`, the CHECK constraint on
  `entities.type` in `db/schema.sql` (existing DBs need `ALTER TABLE entities DROP
  CONSTRAINT entities_type_check, ADD CONSTRAINT entities_type_check CHECK (type IN
  (...))`), and describe the new type in the relevant `skills/*/SKILL.md` contract.
- **New signal types** (e.g. email): add a branch to `signal_detector.py`, write a
  new `skills/<name>/SKILL.md` with the same JSON extraction contract, add it to
  `VALID_SKILLS` and the routing table in `skills/RESOLVER.md`.
- **Swap `manual_docs` for a real API**: `lookup_manual_doc()` returns the same dict
  shape a people-enrichment API would -- replace its internals, nothing downstream
  needs to change.
- **Contradiction detection**: `db.get_all_facts()` (not just `get_latest_facts()`)
  already exposes every fact including superseded ones -- a lint pass could flag
  entities where two sources disagree on the same field.

## Scope notes

Not implemented: cron jobs, the full 20-directory taxonomy, paid people/company
enrichment APIs, notification routing, weekly-lint/maintenance skills, an Open
Threads primitive (the section always renders `[No data yet]` -- there's no DB
table backing it yet). The goal was to validate the core loop -- skill-driven
extraction and composition, deterministic dedupe/tier/persist/render/commit --
which is the architecturally interesting part to replicate.
