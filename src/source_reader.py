"""Reads raw text out of a source file (PDF or plain text) so it can be handed to the
agent. This is the only piece of the old extraction pipeline that's still deterministic
Python -- pulling text out of a PDF isn't a reasoning task, so it doesn't belong in a
skill prompt. Everything after this (which entities are in the text, what facts to
extract) is now the agent's job, guided by skills/*.md.
"""
import hashlib
import mimetypes

from pypdf import PdfReader


def read_source(filepath: str, signal_type: str) -> str:
    if signal_type == "pdf":
        reader = PdfReader(filepath)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        return f.read()


def hash_file(filepath: str) -> str:
    """sha256 of the raw file bytes -- the actual re-ingest dedupe key.
    Hashing bytes (not extracted text) so it's identical regardless of
    signal_type/extraction path, and stable even if pypdf's text extraction
    output ever changes between library versions."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_mime(filepath: str) -> str:
    """Best-effort MIME type from the filename, e.g. 'application/pdf',
    'text/plain'. Stored alongside the hash for display/debugging only --
    content_hash, not mime_type, is what decides "already ingested"."""
    mime, _ = mimetypes.guess_type(filepath)
    return mime or "application/octet-stream"
