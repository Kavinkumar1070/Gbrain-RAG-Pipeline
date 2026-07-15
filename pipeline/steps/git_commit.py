"""
Ingest step 6: commit to git.

Writes the composed page's rendered markdown to disk under the wiki/ directory
(the git-tracked "source of truth"), then commits it if the content actually
changed. Returns a CommitResult so step 7 (Postgres sync) knows the content_hash
and whether there's anything new to chunk/embed, and so ingest.py can log honestly
instead of assuming every run produces a commit.
"""
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommitResult:
    file_path: str           # relative path written, e.g. "wiki/maria-chen.md"
    content_hash: str        # sha256 of the markdown written (feeds pages.content_hash in step 7)
    changed: bool            # False if content was byte-identical to what was already on disk
    committed: bool          # True if a git commit was actually made
    commit_hash: str | None  # short commit sha, None if nothing was committed


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _repo_root(start: Path) -> Path:
    """Find the git repo root by walking up from `start`. Raises if not inside a repo."""
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    if result.returncode != 0:
        raise RuntimeError(f"Not inside a git repository (looked from {start}): {result.stderr.strip()}")
    return Path(result.stdout.strip())


def commit_page(file_path: str, markdown_content: str, repo_root: Path | None = None) -> CommitResult:
    """
    Write `markdown_content` to `file_path` (relative to repo root) and commit it
    if the content changed. `file_path` should match Page.file_path from step 5,
    e.g. "wiki/maria-chen.md".
    """
    repo_root = repo_root or _repo_root(Path.cwd())
    abs_path = repo_root / file_path

    new_hash = hashlib.sha256(markdown_content.encode("utf-8")).hexdigest()

    old_content = abs_path.read_text(encoding="utf-8") if abs_path.exists() else None
    is_new_file = old_content is None
    changed = old_content != markdown_content

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(markdown_content, encoding="utf-8")

    if not changed:
        return CommitResult(file_path, new_hash, changed=False, committed=False, commit_hash=None)

    add_result = _run_git(["add", file_path], cwd=repo_root)
    if add_result.returncode != 0:
        raise RuntimeError(f"git add failed: {add_result.stderr.strip()}")

    action = "add" if is_new_file else "update"
    message = f"gbrain: {action} {file_path}"

    commit_result = _run_git(["commit", "-m", message], cwd=repo_root)
    if commit_result.returncode != 0:
        if "nothing to commit" in commit_result.stdout.lower():
            return CommitResult(file_path, new_hash, changed=True, committed=False, commit_hash=None)
        raise RuntimeError(f"git commit failed: {commit_result.stderr.strip()}")

    sha_result = _run_git(["rev-parse", "--short", "HEAD"], cwd=repo_root)
    commit_hash = sha_result.stdout.strip() if sha_result.returncode == 0 else None

    return CommitResult(file_path, new_hash, changed=True, committed=True, commit_hash=commit_hash)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python git_commit.py <relative_file_path> <content_file>")
        sys.exit(1)

    content = Path(sys.argv[2]).read_text(encoding="utf-8")
    result = commit_page(sys.argv[1], content)
    print(result)