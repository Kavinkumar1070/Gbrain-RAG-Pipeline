"""
Fallback for setup_db.sh if psql CLI isn't installed locally.
Applies init.sql against Supabase and verifies tables/extensions.

Usage: python setup_db.py
"""
import os
import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    with open("init.sql") as f:
        schema_sql = f.read()

    with psycopg.connect(DATABASE_URL, autocommit=True, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            print("Applying schema...")
            cur.execute(schema_sql)

            print("\nTables:")
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
            """)
            for row in cur.fetchall():
                print(f"  - {row[0]}")

            print("\nExtensions:")
            cur.execute("SELECT extname FROM pg_extension;")
            for row in cur.fetchall():
                print(f"  - {row[0]}")

    print("\nDone.")


if __name__ == "__main__":
    main()