"""Run once: python setup_db.py
Creates the pgvector extension and all five tables from db/schema.sql.

WARNING: db/schema.sql now starts with DROP TABLE IF EXISTS ... CASCADE for
every table, so re-running this against a DB that already has data WIPES IT.
This makes schema changes (e.g. new chunks.source_type/chunk_tsv columns)
actually take effect on re-run instead of CREATE TABLE IF NOT EXISTS silently
no-op'ing against the old shape. If you need to keep existing data, don't
run this -- hand-write an ALTER TABLE migration for just the new columns.
"""
from src.db import init_schema

if __name__ == "__main__":
    init_schema()
    print("Schema created successfully.")