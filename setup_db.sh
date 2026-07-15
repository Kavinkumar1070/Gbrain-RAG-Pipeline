#!/usr/bin/env bash
set -euo pipefail

# Loads .env and applies init.sql against Supabase
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL not set. Check .env"
  exit 1
fi

echo "Applying schema..."
psql "$DATABASE_URL" -f init.sql

echo ""
echo "Tables:"
psql "$DATABASE_URL" -c "\dt"

echo ""
echo "Extensions:"
psql "$DATABASE_URL" -c "SELECT extname FROM pg_extension;"