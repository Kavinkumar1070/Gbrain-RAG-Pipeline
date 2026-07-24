"""Entity identity resolution: is this name someone/something already in the brain?

Two-stage match, cheapest first:
  1. Fuzzy string match against canonical_name + aliases (fast, no API call)
  2. Embedding cosine similarity fallback (catches "S. Chen" vs "Sarah Chen")

This replaces gbrain's grep-based dedupe protocol with a DB-backed equivalent.
"""
import numpy as np
from rapidfuzz import fuzz

from src import config
from src.azure_client import embed
from src.db import get_all_entities


def _to_array(v) -> np.ndarray:
    """DB reads come back as pgvector's Vector type (not a numpy array or plain
    list) once register_vector() is active. np.array(vector_obj) does NOT unpack
    it -- Vector has no __iter__/__array__, so numpy silently boxes it as a 0-d
    object array, and any arithmetic on it fails with a TypeError far from the
    real cause. Vector.to_numpy() is the correct way to unwrap it."""
    if hasattr(v, "to_numpy"):
        return v.to_numpy()
    return np.asarray(v, dtype=float)


def _cosine(a, b) -> float:
    a, b = _to_array(a), _to_array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def find_entity(name: str, entity_type: str) -> tuple[str | None, bool]:
    """Returns (entity_id_or_None, matched_via_alias_not_canonical).
    Second value tells the caller whether to add `name` as a new alias."""
    candidates = get_all_entities(entity_type=entity_type)
    if not candidates:
        return None, False

    # Stage 1: fuzzy match
    best_fuzzy_score = 0.0
    best_fuzzy_id = None
    best_is_alias = False

    for c in candidates:
        names_to_check = [c["canonical_name"]] + list(c["aliases"])
        for n in names_to_check:
            score = fuzz.ratio(name.lower(), n.lower())
            if score > best_fuzzy_score:
                best_fuzzy_score = score
                best_fuzzy_id = c["id"]
                best_is_alias = n != c["canonical_name"]

    if best_fuzzy_score >= config.FUZZY_MATCH_THRESHOLD:
        return str(best_fuzzy_id), best_is_alias or (best_fuzzy_score < 100)

    # Stage 2: embedding similarity fallback
    query_vec = embed(name)
    best_cos = -1.0
    best_cos_id = None
    for c in candidates:
        if c["name_embedding"] is None:
            continue
        sim = _cosine(query_vec, c["name_embedding"])
        if sim > best_cos:
            best_cos = sim
            best_cos_id = c["id"]

    if best_cos >= config.EMBEDDING_MATCH_THRESHOLD:
        return str(best_cos_id), True

    return None, False
