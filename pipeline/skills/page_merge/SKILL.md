
# Page Merge Skill

You are reconciling a new document's findings into an existing knowledge base entry (page) for one entity.

You will be given:

- **existing_compiled_truth**: the current narrative summary for this entity, built from prior documents.
- **new_take**: a narrative summary just extracted from a new document about the same entity.
- **new_facts**: a list of atomic facts just extracted from the new document.

Produce a single **merged_compiled_truth**: an updated narrative summary that reflects both the existing knowledge and the new document, as if written fresh with full knowledge of both.

## Rules

- Do not simply concatenate the two summaries. Rewrite into one coherent narrative.
- If new information updates, extends, or corrects something in the existing summary, prefer the new information and reflect the update naturally (e.g. "as of March 2024, X had 50 employees; by January 2025 this grew to 140").
- If new information is unrelated/additive (new facts about a different aspect of the entity), incorporate it without displacing what's already there.
- If new information directly contradicts existing information with no clear resolution (e.g. two different founding years), do not silently pick one — note the discrepancy explicitly in the narrative (e.g. "sources disagree on X").
- Keep it to 3-6 sentences. Do not simply grow the narrative unboundedly across many merges — periodically re-summarize for concision, prioritizing the most current/important information.
- Do not fabricate reconciliation that isn't supported by either source.

## Output

Respond with valid JSON only, matching the provided schema exactly. No preamble, no markdown fences.
