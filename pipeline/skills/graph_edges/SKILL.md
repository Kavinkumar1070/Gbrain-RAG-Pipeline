
# Graph Edges Skill

You are extracting typed relationships (graph edges) between the primary entity of a document and the other entities it mentions (wikilinks), for a personal knowledge base's graph.

You will be given:

- **entity**: the primary entity this document/page is about.
- **take**: a narrative summary of the document.
- **facts**: atomic facts extracted from the document.
- **wikilinks**: a list of other entity names mentioned in the document.

For each wikilink, decide whether the document actually describes a relationship between `entity` and that wikilink, and if so, classify it.

## link_type — pick exactly one, from this fixed list only

- `works_at` — a person is employed by / affiliated with an organization.
- `founded` — a person started the organization.
- `created` — a person or company made/built a product.
- `invested_in` — a funding relationship (investor -> company).
- `acquired` — one company bought another.
- `partnered_with` — a mutual business relationship between two companies (symmetric — direction doesn't matter, pick either order).
- `located_in` — physical presence in a place.
- `part_of` — one thing is a subset/division/component of another (e.g. a product is part of a platform, a team is part of a company).
- `attended` — a person participated in an event.
- `related_to` — use only when a real relationship is clearly stated but doesn't fit any type above. Do not use this as a catch-all for every wikilink.

## direction

`entity` is always the `from` side of the edge as you report it, in the sense that the edge should read naturally as "entity <link_type> wikilink" (e.g. "Maria Chen works_at Nexora"). If the natural relationship actually runs the other way (e.g. the document is a page about Nexora and mentions "founded by Maria Chen"), still report `to_entity: "Maria Chen"` and `link_type: "founded"` — the calling code handles direction correctly using the type's known semantics, you just need to pick the right type and name the correct target entity. Get the type right; don't worry about which literal column it lands in.

## confidence

- `explicit` — the text directly states the relationship.
- `inferred` — a reasonable, low-risk inference from what's stated (e.g. text says someone "led the round that closed the acquisition" -> `acquired` between the two companies named, `inferred`).

Never fabricate a relationship that isn't grounded in the text at all, regardless of confidence label.

## evidence

A short verbatim quote or close paraphrase (one sentence or less) from the text that supports this edge. This is for debugging — a human should be able to read `evidence` and immediately see why the edge was extracted.

## What to omit

Not every wikilink needs an edge. If a wikilink is mentioned only in passing with no describable relationship to `entity` (e.g. listed alongside others with no stated connection), omit it from `edges` entirely rather than forcing `related_to`. It's fine — expected, even — for `edges` to be shorter than `wikilinks`.

## Output

Respond with valid JSON only, matching the provided schema exactly. No preamble, no markdown fences. If no wikilink yields a real edge, return `{"edges": []}`.
