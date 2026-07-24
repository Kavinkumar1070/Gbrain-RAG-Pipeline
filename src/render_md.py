"""Regenerates a .md page for an entity from the DB. Markdown is a *view*, never
edited by hand and never the source of truth -- re-running this always overwrites
the file cleanly.

Section layout is per entity-type, per gbrain's schema doc: a person page is not
the same shape as a company page, which is not the same shape as a meeting page.
Previously every type rendered through one generic TEMPLATE (State/Relationships/
Open Threads/Timeline) -- that's gbrain's Company shape stretched over every type.
A meeting isn't "State" + "Timeline"; it's Attendees, Key Decisions, Action Items,
and a Full Transcript below the line. A person page's highest-value sections
(What They Believe, Communication Style) aren't generic "State" facts either --
gbrain calls them out as distinct, sourced, hedged sections on purpose.

The only place the model is involved is `_compose_summary` (one structured skill
call, no tools, no filesystem access for the model). Every field it's given comes
from the DB; the write to disk happens here in Python regardless of what the model
returns -- if the skill call fails, we fall back to a deterministic one-liner
rather than leaving the page half-rendered.
"""
import json
import os

from src import config, db
from src.resolver import resolve_folder
from src.skill_runner import run_skill

# ---------------------------------------------------------------------------
# Per-type section layout: which fact fields feed which named section, for
# entity types where "State" isn't the only bucket. Order matters -- it's the
# order sections render in. Anything NOT matched by a set below falls back to
# the generic "State" section, so new/unrecognized fields never silently
# disappear -- they just land in State until someone teaches a type about them.
# ---------------------------------------------------------------------------
FIELD_SECTIONS: dict[str, list[tuple[str, set[str]]]] = {
    "person": [
        ("What They Believe", {"belief", "beliefs", "worldview", "position"}),
        ("What They're Building", {"building", "project", "shipping"}),
        ("What Motivates Them", {"motivation", "motivates", "ambition"}),
        ("Communication Style", {"communication_style", "communication"}),
    ],
    "company": [
        ("Key People", {"key_people", "ceo", "founder", "leadership"}),
        ("Key Metrics", {"revenue", "headcount", "funding", "valuation", "stage"}),
    ],
    "deal": [
        ("Terms", {"terms", "amount", "valuation", "round", "stage"}),
        ("Parties", {"investor", "investors", "lead_investor", "parties"}),
    ],
    # "meeting" and "project" intentionally absent -- meeting has its own
    # template below; project falls back to the generic layout via .get().
}

GENERIC_TEMPLATE = """# {name}

> {summary}

## State
{state_lines}

## Relationships
{relationship_lines}

{extra_sections}## Open Threads
{open_threads}

## Fact History
{fact_history_lines}

---

## Timeline
{timeline_lines}
"""

MEETING_TEMPLATE = """# {name}

> {summary}

## Attendees
{attendees}

## Key Decisions
{key_decisions}

## Action Items
{action_items}

## Connections
{connections}

## Fact History
{fact_history_lines}

---

## Timeline
{timeline_lines}

## Full Transcript
{transcript}
"""


def _format_fact_history(all_facts: list[dict]) -> str:
    """Renders EVERY fact ever inserted for this entity, not just the
    latest-per-field values State/extra-sections show. This is the
    append-only counterpart to those sections -- per gbrain's principle that
    the committed .md file is the source of truth (the DB is a queryable
    index over it, not the other way around): if facts.py's DB rows were
    lost, the only place a superseded or contradicting value survives is
    here. Never rewritten, only grown -- same spirit as Timeline.

    Rendered above the `---` divider, not below it: it's still a *compiled*
    view (grouped by field, current value marked), just a fuller one than
    State/the type-specific sections show. Only Timeline -- the raw,
    ungrouped, append-only event log -- belongs below the line.

    Grouped by field, newest-first within each field, with the current
    (latest) value per field marked so a reader can tell at a glance which
    ones State/the type-specific sections are currently showing.
    """
    if not all_facts:
        return "- [No data yet]"

    by_field: dict[str, list[dict]] = {}
    for f in all_facts:
        by_field.setdefault(f["field"], []).append(f)

    blocks = []
    for field in sorted(by_field):
        entries = by_field[field]  # already ordered field, observed_at DESC from db.get_all_facts()
        lines = []
        for i, info in enumerate(entries):
            date = info["observed_at"].strftime("%Y-%m-%d")
            marker = " _(current)_" if i == 0 else " _(superseded)_"
            lines.append(
                f"  - {info['value']}  _(source: {info['source']}, {info['confidence']}, {date})_{marker}"
            )
        blocks.append(f"- **{field.replace('_', ' ').title()}:**\n" + "\n".join(lines))

    return "\n".join(blocks)


def _format_state(facts: dict, exclude_fields: set[str] = frozenset()) -> str:
    remaining = {f: v for f, v in facts.items() if f not in exclude_fields}
    if not remaining:
        return "- [No data yet]"
    lines = []
    for field, info in sorted(remaining.items()):
        lines.append(f"- **{field.replace('_', ' ').title()}:** {info['value']}  _(source: {info['source']}, {info['confidence']})_")
    return "\n".join(lines)


def _format_extra_sections(facts: dict, entity_type: str) -> tuple[str, set[str]]:
    """Builds the type-specific sections (e.g. person's 'What They Believe').
    Returns the rendered block plus the set of field names it consumed, so
    the caller's State section only shows what's left over."""
    sections = FIELD_SECTIONS.get(entity_type, [])
    if not sections:
        return "", set()

    used_fields: set[str] = set()
    blocks = []
    for header, field_names in sections:
        matched = {f: v for f, v in facts.items() if f in field_names}
        used_fields |= matched.keys()
        if matched:
            lines = "\n".join(
                f"- {info['value']}  _(source: {info['source']}, {info['confidence']})_"
                for info in matched.values()
            )
        else:
            lines = "- [No data yet]"
        blocks.append(f"## {header}\n{lines}\n")

    return "\n".join(blocks) + "\n", used_fields


def _format_relationships(rels: list[dict], relation_filter=None) -> str:
    """direction 'out' -> this entity did the action (-> arrow).
    direction 'in'  -> another entity's action points at this one (<- arrow),
    i.e. the backlink gbrain's schema calls for."""
    if relation_filter is not None:
        rels = [r for r in rels if r["relation_type"] in relation_filter]
    if not rels:
        return "- [No data yet]"
    lines = []
    for r in rels:
        folder = resolve_folder(r["type"])
        arrow = "→" if r["direction"] == "out" else "←"
        lines.append(f"- **{r['relation_type']}** {arrow} [{r['name']}]({folder}/{r['slug']}.md)")
    return "\n".join(lines)


def _format_timeline(events: list[dict]) -> str:
    if not events:
        return "- [No data yet]"
    lines = []
    for e in events:
        date = e["observed_at"].strftime("%Y-%m-%d")
        lines.append(f"- **{date}** | {e['source_type']} ({e['confidence']}) — {e['content']}")
    return "\n".join(lines)


def _format_fact_field(facts: dict, field_names: set[str]) -> str:
    matched = [info["value"] for f, info in facts.items() if f in field_names]
    if not matched:
        return "- [No data yet]"
    return "\n".join(f"- {v}" for v in matched)


def _fallback_summary(facts: dict) -> str:
    """Deterministic summary used if the compose-page skill call fails or
    returns nothing usable -- rendering must never hard-fail on a bad LLM response."""
    role = facts.get("role", {}).get("value")
    company = facts.get("company", {}).get("value")
    if role and company:
        return f"{role} at {company}."
    if role:
        return f"{role}."
    return "[No summary yet -- needs enrichment]"


def _compose_summary(name: str, entity_type: str, facts: dict, events: list[dict]) -> str:
    payload = json.dumps({
        "name": name,
        "type": entity_type,
        "facts": {
            field: {**info, "observed_at": str(info["observed_at"])}
            for field, info in facts.items()
        },
        "recent_events": [
            {**e, "observed_at": str(e["observed_at"])}
            for e in events[:5]
        ],
    })
    result = run_skill("skills/compose-page/SKILL.md", payload, json_mode=True)
    if result and result.get("summary"):
        return result["summary"]
    return _fallback_summary(facts)


def _read_transcript(events: list[dict]) -> str:
    """Meeting pages show the raw transcript below the line, per gbrain's
    Meeting template. The transcript IS the immutable source file -- we read
    it from the same source_ref events already point at rather than copying
    it into the DB a second time."""
    for e in events:
        ref = e.get("source_ref")
        if ref and os.path.isfile(ref):
            with open(ref, encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
    return "[Transcript not available]"


def _render_meeting(entity: dict, facts: dict, all_facts: list[dict], events: list[dict], rels: list[dict]) -> str:
    return MEETING_TEMPLATE.format(
        name=entity["canonical_name"],
        summary=_compose_summary(entity["canonical_name"], entity["type"], facts, events),
        attendees=_format_relationships(rels, relation_filter={"attended"}),
        key_decisions=_format_fact_field(facts, {"decision", "decisions"}),
        action_items=_format_fact_field(facts, {"action_item", "action_items"}),
        connections=_format_relationships(rels, relation_filter=None) if rels and any(
            r["relation_type"] != "attended" for r in rels
        ) else "- [No data yet]",
        timeline_lines=_format_timeline(events),
        fact_history_lines=_format_fact_history(all_facts),
        transcript=_read_transcript(events),
    )


def _render_generic(entity: dict, facts: dict, all_facts: list[dict], events: list[dict], rels: list[dict]) -> str:
    extra_block, used_fields = _format_extra_sections(facts, entity["type"])
    return GENERIC_TEMPLATE.format(
        name=entity["canonical_name"],
        summary=_compose_summary(entity["canonical_name"], entity["type"], facts, events),
        state_lines=_format_state(facts, exclude_fields=used_fields),
        relationship_lines=_format_relationships(rels),
        extra_sections=extra_block,
        open_threads="- [No data yet]",
        timeline_lines=_format_timeline(events),
        fact_history_lines=_format_fact_history(all_facts),
    )


def render_entity_page(entity_id: str) -> str:
    entity = db.get_entity(entity_id)
    if entity is None:
        raise ValueError(f"No entity found for id {entity_id}")

    facts = db.get_latest_facts(entity_id)
    all_facts = db.get_all_facts(entity_id)
    events = db.get_events(entity_id)
    rels = db.get_relationships(entity_id)

    if entity["type"] == "meeting":
        content = _render_meeting(entity, facts, all_facts, events, rels)
    else:
        content = _render_generic(entity, facts, all_facts, events, rels)

    folder = resolve_folder(entity["type"])
    out_dir = os.path.join(config.GIT_REPO_PATH, folder)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{entity['slug']}.md")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    return out_path