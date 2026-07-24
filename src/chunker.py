"""Splits raw source text into retrieval chunks. Deterministic, no LLM call --
same category as source_reader.py: pulling text apart by size isn't a
reasoning task. Embedding each chunk still costs an API call (azure_client.embed),
but the decision of *where* to cut does not need the model.

Paragraph-aware: prefers to cut on blank lines so a chunk doesn't split a
speaker turn or a sentence in half where avoidable. Falls back to a hard
character cut only if a single paragraph itself exceeds max_chars.
"""

DEFAULT_MAX_CHARS = 1200  # ~250-300 tokens; keeps each chunk small enough that
                           # a retrieved chunk is still specific, not a whole document


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            # a single paragraph is itself too big -- hard-cut it, flushing
            # whatever was accumulating first
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > max_chars:
            chunks.append(current)
            current = para
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks