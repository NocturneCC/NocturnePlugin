"""Trust gate for deployment commands executed from a Git checkout."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess


FULL_SHA = re.compile(r"[0-9a-f]{40}")


def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=30, **kwargs)


def verify_checkout(repo, commit, *, run=command):
    repo = Path(repo).resolve(strict=True)
    if not isinstance(commit, str) or FULL_SHA.fullmatch(commit) is None:
        raise ValueError("an exact lowercase full Git commit SHA is required")
    top = Path(run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                   stdout=subprocess.PIPE, text=True).stdout.strip()).resolve(strict=True)
    if top != repo:
        raise ValueError("deployment repository is not the exact Git worktree root")
    head = run(["git", "-C", str(repo), "rev-parse", "HEAD"],
               stdout=subprocess.PIPE, text=True).stdout.strip()
    if head != commit:
        raise ValueError("deployment checkout HEAD does not equal the requested commit")
    status = run(["git", "-C", str(repo), "status", "--porcelain=v1",
                  "--untracked-files=all"], stdout=subprocess.PIPE, text=True).stdout
    if status:
        raise ValueError("deployment checkout is dirty")
    resolved = run(["git", "-C", str(repo), "rev-parse", "--verify",
                    commit + "^{commit}"], stdout=subprocess.PIPE, text=True).stdout.strip()
    if resolved != commit:
        raise ValueError("requested deployment commit is missing or ambiguous")
    return {"repo": str(repo), "commit": commit, "clean": True}
