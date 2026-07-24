---
name: compose-page
type: composition
triggers: called once per touched entity, after all DB writes for it are done
---

# compose-page

You will be given an entity's current state as JSON: its name, type,
structured facts (each with a source and confidence), and its recent
timeline events. Write a one-paragraph executive summary for the top of its
brain page — the kind of thing someone reads in 10 seconds before a meeting.

## Rules

- Ground every sentence in the given facts/events. Do not add anything not
  present in the input.
- Hedge appropriately: a fact tagged `inferred` or based on a single event
  should read as tentative, not settled. A `self-described` fact can be
  stated as their own claim ("says she's..."), not as verified truth.
- If facts are sparse, write a short, honest summary rather than padding it
  — "Thin profile so far: <the one or two things known>." is better than
  invented color.
- Plain prose, 1-3 sentences. No headers, no bullet points, no markdown.

## Output contract

Output ONLY JSON, no prose outside the JSON, no markdown fences:

```json
{"summary": "One to three sentences."}
```
