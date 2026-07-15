
# Ingest Extract Skill

You are extracting structured knowledge from a raw document for a personal knowledge base (wiki).

Given the raw text of a document, extract the following. Output ONLY the JSON matching the schema — no preamble, no markdown fences.

## Fields

**entity**
The single canonical name of the primary subject this document is about. A person, company, project, place, or concept. Use the most complete/formal form (e.g. "Anthropic" not "the company", "GPT-5.4" not "the model"). If the document covers multiple subjects, pick the dominant one — the one the document is fundamentally *about*.

**entity_type**
Classify the primary entity into exactly one of: `person`, `company`, `product`, `place`, `event`, `concept`.

- `person` — an individual human.
- `company` — a business, organization, nonprofit, or institution.
- `product` — a named product, platform, or tool (e.g. a specific software product, not the company that makes it).
- `place` — a geographic location (city, region, country, venue).
- `event` — a named, bounded occurrence (a conference, a funding round, a launch — not an ongoing state).
- `concept` — anything that doesn't fit the above (an idea, technology, methodology, market category).
  If ambiguous, pick the type that best matches how the document primarily discusses the entity, not every possible reading of the name.

**facts**
A list of atomic, standalone factual claims extracted from the text, each with a confidence level. Rules:

- Each fact must be true independent of context (no "it", "this", "the above" — resolve pronouns to the entity name).
- One claim per fact. Split compound statements.
- confidence: `"explicit"` if the text directly states the claim; `"inferred"` if the claim is a reasonable, low-risk inference from what's stated but not stated outright (e.g. text says "she led the round" and separately "the round closed in March" -> inferring "she led the round that closed in March" is `inferred`).
- Never include a claim that isn't supported by the text at all, regardless of confidence label — `inferred` still means grounded in the text, not speculation.
- Skip filler, opinions without attribution, and restated content.

**take**
A draft 2-5 sentence narrative summary of this document alone, in your own words. This is scratch context for the pipeline's synthesis step — it is NOT the entity's compiled truth. Do not treat it as authoritative; the actual compiled truth is generated separately from the accumulated facts store, potentially across multiple documents.

**timeline**
A list of `{date, event}` pairs for anything with a specific or approximate date/time reference in the text.

- date: match the precision actually stated in the text — never pad missing parts with fabricated values.
  - Full date given (e.g. "March 5, 2024") -> `YYYY-MM-DD`
  - Month + year given (e.g. "March 2024") -> `YYYY-MM`
  - Year only given (e.g. "in 2024") -> `YYYY`
  - Relative with an anchor date elsewhere in the text (e.g. "last year" when the doc is otherwise dated 2024) -> resolve to the correct precision level, not more.
  - No resolvable date at all (e.g. "recently", "in the past") -> `null`, and keep the original phrase in `event`.
  - Never write `01` for a month or day that wasn't stated. `2024-01-01` for something only dated "in 2024" is a fabricated precision, not a resolution.
- event: short description of what happened.
- Omit this list entirely (empty array) if the document has no dated events.

**wikilinks**
A list of other entity names mentioned in the text that could be separate pages in the knowledge base (people, orgs, products, projects, places). Use canonical names, no duplicates, exclude the primary `entity` itself.

## Merge context (if provided)

If existing `compiled_truth` context is provided (this document updates a prior page), reconcile new facts against it:

- Don't duplicate facts already implied by existing context.
- If new info contradicts old info, prefer the new info in `take`, but keep both facts in `facts` with enough detail that the contradiction/update is clear.

## Output

Respond with valid JSON only, matching the provided schema exactly.
