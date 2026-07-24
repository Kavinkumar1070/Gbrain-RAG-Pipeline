
---
name: media-ingest
type: extraction (generic fallback -- everything that isn't a transcript)
triggers: PDFs, reports, decks, articles, plain notes, email bodies, any non-transcript text
---
# media-ingest

You will be given extracted text — a document, a note, an email body,
whatever isn't a transcript. Extract every specifically-named entity that is
an actual subject of the text, not a passing citation or boilerplate.

The entity types this brain currently recognizes are: {{ENTITY_TYPES}}.
Extract entities of any of those types that are actually present in the
text (a person, a company, a project being built, a deal with terms, or any
custom type this brain has been configured with). Never invent a type that
isn't in that list.

`meeting` does not apply here even if it's in the recognized list above —
that type only applies to transcripts (`meeting-ingestion`).

## Rules

- One narrative `fact` sentence per entity, grounded in the text. This goes
  to the entity's Timeline (append-only event log).
- Additionally, if the text states a *specific, structured* value about an
  entity — role, company, stage, funding, a belief, what they're building —
  put it under `facts` as `field: value` pairs (this feeds the compiled-truth
  State / type-specific sections). Omit the key if nothing specific is
  stated; never guess to fill it in. Use `role`/`company` for a person's
  job/employer, `belief`/`building`/`motivation`/`communication_style` for
  person context, `stage`/`funding` for company/deal specifics — add other
  keys freely when nothing above fits.
- Do not extract entities from headers, footers, or navigation boilerplate.
- Do not treat the document's author as an entity unless the document is
  actually about them.
- Skip generic mentions — "the team", "some investors" don't count.
- If the text states a relationship between two extracted entities (an org
  chart, "X, CEO of Y", "raised a Series A from Z"), include it under `related`.

## Output contract

Output ONLY JSON, no prose, no markdown fences:

```json
{
  "entities": [
    {"name": "...", "type": "...", "fact": "...",
     "confidence": "observed" | "self-described" | "inferred",
     "facts": {"role": "...", "company": "..."},
     "related": [{"name": "...", "type": "...", "relation_type": "..."}]}
  ]
}
```

`type` (both top-level and inside `related`) must be one of: {{ENTITY_TYPES}}.
`related[].relation_type` should prefer this brain's existing edge vocabulary
when it fits: {{RELATION_TYPES}} — but if none of those accurately describes
the relationship stated in the text, use a short, clear `snake_case` type of
your own rather than forcing a bad fit. `facts` may be `{}` or omitted if
nothing structured was actually stated.