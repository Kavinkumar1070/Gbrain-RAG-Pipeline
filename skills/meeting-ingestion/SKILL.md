
---
name: meeting-ingestion
type: extraction
triggers: transcripts, call recordings with speaker turns
---
# meeting-ingestion

You will be given the full text of a meeting transcript. The entity types
this brain currently recognizes are: {{ENTITY_TYPES}}. Extract:

1. **The meeting itself** — exactly one entity with `type: "meeting"` (if
   `"meeting"` isn't in the recognized type list above, skip this entity
   entirely rather than inventing an unrecognized type). `name` is a short
   title (infer one from the subject matter if none is stated, e.g.
   "Product Review — API Stability & Q3 Launch"). `fact` is a one or two
   sentence summary of what the meeting covered and what was decided.
   `related` lists every attendee (`relation_type: "attended"`) and every
   company discussed (`relation_type: "discussed"`).
2. **Every other entity** of a recognized type (from the list above, e.g.
   person, company, project, deal, or any custom type this brain has been
   configured with) that is named or discussed — a speaker, a company, a
   project being built, a deal with terms, etc. Only use types from the
   recognized list; never invent a type that isn't in it.

## Rules

- One narrative `fact` sentence per entity, grounded in what was actually
  said. Never invent sentiment or belief that wasn't expressed. This goes to
  the entity's Timeline (append-only event log).
- Additionally, if the transcript states any *specific, structured* value
  about an entity — a role, a company, a decision, an action item, a belief,
  what they're building — put it under `facts` as `field: value` pairs. This
  goes to the entity's compiled-truth State (or a type-specific section like
  "What They're Building"). Do not force a `facts` entry that isn't actually
  stated; omit the key entirely rather than guessing. `fact` and `facts` are
  not redundant: `fact` is one sentence for the record; `facts` is whatever
  discrete structured values that sentence actually contains, if any.
  Recognized field names (use these exact keys when applicable, add others
  freely when nothing above fits):
  - `role`, `company` — person's stated job/role and employer
  - `decision`, `action_item` — on the **meeting** entity only, one key
    per distinct decision/action item stated (if more than one, still use
    the single `decision`/`action_item` key — Python groups by field name,
    not by uniqueness, so pick the single most important one per entity
    per ingest and let repeated ingests build the Timeline)
  - `belief`, `building`, `motivation`, `communication_style` — person
    context sections, only if actually stated, never inferred from tone
  - `stage`, `funding` — company/deal specifics
- Do not skip minor mentions — even a name mentioned once still gets an
  entry (Python decides tiering downstream, not you).
- If the transcript states a relationship between two entities you're
  extracting (e.g. "Alex is at Meridian Labs", "Sarah and Mike co-lead the
  launch"), include it under `related` on the relevant entity.
- Do not summarize the whole meeting into one entity's fact — each entity
  gets its own sentence, specific to what concerns them. The meeting entity
  is the one place a broader summary belongs.
- `confidence` is `observed` unless the entity is describing themselves
  ("I'm joining Acme next month" → self-described) or you're reading
  between the lines (inferred). This confidence applies to `facts` values
  too, per-field if they differ — but keep it simple: default to the same
  confidence as `fact` unless one specific field genuinely warrants a
  different label.
- Not every meeting has a project or deal in it — omit those entities
  entirely rather than forcing one.

## Output contract

Output ONLY JSON, no prose, no markdown fences:

```json
{
  "entities": [
    {
      "name": "Product Review — API Stability & Q3 Launch",
      "type": "meeting",
      "fact": "Reviewed Q3 launch readiness; agreed to hold for another load-testing pass before shipping.",
      "confidence": "observed",
      "facts": {
        "decision": "Hold Q3 launch until another load-testing pass is done.",
        "action_item": "QA to prioritize load testing this week."
      },
      "related": [
        {"name": "Sarah Chen", "type": "person", "relation_type": "attended"},
        {"name": "Mike", "type": "person", "relation_type": "attended"}
      ]
    },
    {
      "name": "Sarah Chen",
      "type": "person",
      "fact": "Flagged unresolved API stability issues, wants another load-testing pass before Q3 launch.",
      "confidence": "observed",
      "facts": {
        "belief": "Believes the Q3 launch should not ship until API stability is confirmed via another load-testing round."
      },
      "related": [
        {"name": "Acme", "type": "company", "relation_type": "works_at"}
      ]
    }
  ]
}
```

`type` must be one of: {{ENTITY_TYPES}}. `related[].relation_type` should
prefer this brain's existing edge vocabulary when it fits: {{RELATION_TYPES}}
— but if none of those accurately describes the relationship stated in the
transcript, use a short, clear `snake_case` type of your own rather than
forcing a bad fit. `related` may be an empty list. `facts` may be an empty
object `{}` or omitted entirely if nothing structured was actually stated.