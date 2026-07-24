"""Commits changes in GIT_REPO_PATH after each ingest run. Batches all page writes from
one ingest call into a single commit, per gbrain's concurrency guidance."""
import os
from git import Repo, InvalidGitRepositoryError

from src import config


def _get_or_init_repo() -> Repo:
    path = config.GIT_REPO_PATH
    os.makedirs(path, exist_ok=True)
    try:
        return Repo(path)
    except InvalidGitRepositoryError:
        repo = Repo.init(path)
        return repo


def commit_wiki(message: str):
    repo = _get_or_init_repo()
    repo.git.add(A=True)
    if repo.is_dirty() or repo.untracked_files:
        repo.index.commit(message)
        print(f"[git] committed: {message}")
    else:
        print("[git] nothing to commit")
