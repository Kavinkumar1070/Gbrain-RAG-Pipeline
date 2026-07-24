# RESOLVER.md — skill router

You will be given the raw text of an incoming signal (a file's extracted
content). Decide which extraction skill should process it. There are only
two: a signal either has the shape of a transcript, or it doesn't.

Do not confuse this with the entity resolver (folder routing), which is
plain Python (`src/resolver.py`) and only fires when creating a brand-new
entity page.

## Decision table

| Signal looks like | Route to |
|---|---|
| A transcript — repeated `Name:` speaker turns, a call/meeting record | `meeting-ingestion` |
| Anything else — a document, PDF text, plain notes, an email body | `media-ingest` |

## Output contract

Output ONLY JSON, no prose, no markdown fences:

```json
{"skill": "meeting-ingestion" | "media-ingest"}
```

If you are genuinely unsure, prefer `media-ingest` — it is the generic
fallback and still calls the same per-entity persistence path.
