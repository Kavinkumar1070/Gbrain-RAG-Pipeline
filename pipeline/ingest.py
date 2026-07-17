"""
Ingest orchestrator — wired through step 8 (graph edges).

Usage: python pipeline/ingest.py <file.pdf|file.docx|file.txt>

Requires WIKI_REPO_PATH in .env, pointing at a separate git repo (not this
pipeline's own repo) that holds the wiki/*.md files. See git_commit.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from steps import extract, llm_pass, brainops, page_write, git_commit, postgres_sync, graph_edges


def run_ingest(file_path: str):
    print(f"[1-2] Extracting: {file_path}")
    raw_text = extract.extract(file_path)
    print(f"      -> {len(raw_text)} chars extracted\n")

    print("[3] Running LLM pass...")
    result = llm_pass.run(raw_text)
    print(f"      -> done (entity: {result.entity}, type: {result.entity_type})\n")

    print("[4] Brain-ops lookup...")
    brain_result = brainops.lookup(result.entity, entity_type=result.entity_type)
    if brain_result.exists and brain_result.compiled_truth:
        print(f"      -> entity exists (id={brain_result.entity_id}), merge context available for step 5")
    elif brain_result.exists:
        print(f"      -> entity exists (id={brain_result.entity_id}), but no prior page yet — no merge context")
    else:
        print(f"      -> new entity created (id={brain_result.entity_id}), no prior context")

    print("\n[5] Page write...")
    page = page_write.run(result, brain_result)
    print(f"      -> composed page for {page.file_path}\n")

    print("[6] Commit to git...")
    # NOTE: save_raw_sidecar always writes+stages a new timestamped file (raw
    # text differs per source doc even if the synthesized page doesn't). If
    # commit_page below finds the page itself unchanged, it skips the commit
    # and this staged sidecar sits uncommitted until the next real change.
    # Fine for now; revisit if that gap matters.
    raw_rel_path = git_commit.save_raw_sidecar(page.file_path, raw_text, file_path)
    print(f"      -> raw source saved to {raw_rel_path}")
    markdown = page_write.render_markdown(page)
    commit_result = git_commit.commit_page(page.file_path, markdown)
    if commit_result.committed:
        print(f"      -> committed {commit_result.file_path} ({commit_result.commit_hash})\n")
    elif commit_result.changed:
        print(f"      -> wrote {commit_result.file_path}, but git reported nothing staged\n")
    else:
        print(f"      -> {commit_result.file_path} unchanged, skipped commit\n")

    print("[7] Syncing to Postgres...")
    # raw_text/raw_rel_path threaded through so postgres_sync can embed the
    # untouched source text as 'raw' content_chunks (searchable even if the
    # LLM pass dropped or paraphrased something) and stamp facts/timeline
    # rows with their source_doc for citations -- these are computed above
    # in the right order already, no reshuffling needed.
    sync_result = postgres_sync.sync(page, commit_result, raw_text=raw_text, raw_rel_path=raw_rel_path)
    if sync_result.skipped:
        print(f"      -> no new facts/timeline/chunks; touch recorded (event={sync_result.event_id})\n")
    else:
        print(f"      -> {sync_result.facts_written} facts, {sync_result.timeline_written} timeline entries, "
              f"{sync_result.chunks_written} chunks embedded (event={sync_result.event_id})\n")

    print("[8] Extracting graph edges...")
    edges = graph_edges.run(result.entity, result.take, result.facts, result.wikilinks)
    edge_result = graph_edges.sync(page.entity_id, edges, content_changed=commit_result.changed)
    if edge_result.skipped:
        print("      -> skipped, content unchanged\n")
    else:
        print(f"      -> {edge_result.edges_written} edges written "
              f"({len(edges) - edge_result.edges_written} dropped as self-links)\n")

    return result, brain_result, page, commit_result, sync_result, edge_result


def _print_result(result):
    print(f"Entity: {result.entity} ({result.entity_type})\n")
    print(f"Take:\n{result.take}\n")
    print(f"Facts ({len(result.facts)}):")
    for f in result.facts:
        print(f"  - {f}")
    print(f"\nTimeline ({len(result.timeline)}):")
    for t in result.timeline:
        print(f"  - {t.date}: {t.event}")
    print(f"\nWikilinks: {result.wikilinks}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python pipeline/ingest.py <file.pdf|file.docx|file.txt>")
        sys.exit(1)

    result, brain_result, page, commit_result, sync_result, edge_result = run_ingest(sys.argv[1])
    #_print_result(result)
    #print(f"\n--- Page written to {commit_result.file_path} in the wiki repo ---")