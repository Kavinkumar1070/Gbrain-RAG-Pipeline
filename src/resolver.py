"""Entity resolver -- gbrain docs' 'Resolver' section equivalent.

Signal routing (which skill handles a .pdf/.txt/transcript) is NOT here.
That's the RESOLVER.md skill call in run_pipeline.py (a single structured
LLM call, not a Python function) -- do not add a route_to_skill() here.

This module only answers the second, narrower question: once the pipeline
has decided an entity is brand new, which brain folder does it belong in?

The type -> folder map is NOT hardcoded here anymore -- it's read from
config.ENTITY_TYPES, which is parsed from the ENTITY_TYPES env var. Add a
new type by editing .env, not this file.
"""
from src import config


def resolve_folder(entity_type: str) -> str:
    """Minimal version of gbrain's 20-branch decision tree in
    docs/GBRAIN_RECOMMENDED_SCHEMA.md. Looks up config.ENTITY_TYPES (built
    from the ENTITY_TYPES env var) -- extend .env, not this function, to
    add deals/, projects/, concepts/, etc."""
    folder = config.ENTITY_TYPES.get(entity_type)
    if folder is None:
        return "inbox"  # unrecognized entity types fall back to inbox, per gbrain convention
    return folder