"""Loads environment variables. Import this before anything else that needs config."""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill in your credentials."
        )
    return val


AZURE_OPENAI_ENDPOINT = _require("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = _require("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_VERSION = _require("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_DEPLOYMENT = _require("AZURE_OPENAI_DEPLOYMENT")
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = _require("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

DATABASE_URL = _require("DATABASE_URL")
GIT_REPO_PATH = os.environ.get("GIT_REPO_PATH", "./wiki")

MANUAL_DOCS_DIR = os.environ.get("MANUAL_DOCS_DIR", "./enrichment_docs")
SOURCES_DIR = os.environ.get("SOURCES_DIR", "./sources")

# Dedupe thresholds
FUZZY_MATCH_THRESHOLD = float(os.environ.get("FUZZY_MATCH_THRESHOLD", "85"))
EMBEDDING_MATCH_THRESHOLD = float(os.environ.get("EMBEDDING_MATCH_THRESHOLD", "0.87"))

# Tier escalation thresholds (number of events before auto-upgrading tier)
TIER2_EVENT_COUNT = int(os.environ.get("TIER2_EVENT_COUNT", "2"))
TIER1_EVENT_COUNT = int(os.environ.get("TIER1_EVENT_COUNT", "4"))

# ---------------------------------------------------------------------------
# Dynamic entity / edge (relationship) types.
#
# This is the single source of truth for "what kinds of things can this
# brain know about" and "what kinds of edges can connect them." Every other
# module (resolver.py, db/schema.sql via init_schema(), the SKILL.md prompts
# via skill_runner.py) reads from THESE two values instead of hardcoding a
# type list. To add/remove/rename a type, edit .env and re-run
# `python setup_db.py` -- no source file needs to change.
#
# ENTITY_TYPES format: "type:folder,type:folder,..."
#   e.g. "person:people,company:companies,concept:concepts"
#   `type` is what the LLM writes in its JSON output and what's stored in
#   entities.type. `folder` is the wiki/<folder>/ subdirectory pages of that
#   type render into.
#
# RELATION_TYPES format: "relation_type,relation_type,..."
#   e.g. "works_at,attended,discussed,co_founded,invested_in,knows"
#   relationships.relation_type has no DB constraint (free TEXT) by design --
#   a graph edge type is cheap to invent and doesn't need a folder or a
#   render template the way an entity type does. This list is purely
#   *vocabulary guidance* injected into the extraction skill prompts so the
#   model prefers your existing edge types over inventing near-duplicates
#   ("works_at" vs "worksAt" vs "employed_by"). The model may still emit one
#   outside this list; it isn't rejected, just flagged in the pipeline log.
# ---------------------------------------------------------------------------
_DEFAULT_ENTITY_TYPES = "person:people,company:companies,meeting:meetings,project:projects,deal:deals"
_DEFAULT_RELATION_TYPES = "works_at,attended,discussed,co_founded,invested_in,knows"

ENTITY_TYPES: dict[str, str] = dict(
    pair.split(":", 1)
    for pair in os.environ.get("ENTITY_TYPES", _DEFAULT_ENTITY_TYPES).split(",")
    if pair.strip()
)

RELATION_TYPES: list[str] = [
    r.strip() for r in os.environ.get("RELATION_TYPES", _DEFAULT_RELATION_TYPES).split(",") if r.strip()
]