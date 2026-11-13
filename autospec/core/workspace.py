"""Workspaces, git worktrees and git helpers (§10). All git calls use argument lists (L1)."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BOT_NAME = "AutoSpecBot"
BOT_EMAIL = "bot@local.dev"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class GitError(RuntimeError):
    pass


class PathViolation(ValueError):
    pass


def git(args: Sequence[str], cwd: Path, *, check: bool = True, timeout: int = 120) -> str:
    argv = ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", *args]
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:800]}")
    return proc.stdout


def safe_join(root: Path, rel: str | Path) -> Path:
    """Resolve rel under root; reject traversal and absolute paths outside root."""
    root_r = root.resolve()
    p = (root_r / rel).resolve()
    if p != root_r and root_r not in p.parents:
        raise PathViolation(f"path escapes the workspace: {rel}")
    return p


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid workspace name: {name!r}")
    return name


@dataclass
class Workspace:
    name: str
    root: Path

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def has_commits(self) -> bool:
        return subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.root,
                              capture_output=True).returncode == 0

    def ensure_repo(self, *, set_identity: bool = False) -> None:
        """git init when needed and make sure HEAD exists (workspace-scoped, never cwd-dependent: B8)."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.is_repo():
            git(["init", "-q"], self.root)
            set_identity = True
        if set_identity:
            git(["config", "user.name", BOT_NAME], self.root)
            git(["config", "user.email", BOT_EMAIL], self.root)
        if not self.has_commits():
            git(["commit", "-q", "--allow-empty", "-m", "autospec: initial commit"], self.root)

    def exclude_locally(self, patterns: Sequence[str]) -> None:
        """Add patterns to .git/info/exclude (shared by worktrees, never committed, no tracked file changes)."""
        common = Path(git(["rev-parse", "--git-common-dir"], self.root).strip())
        if not common.is_absolute():
            common = self.root / common
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        missing = [p for p in patterns if p not in current]
        if missing:
            with exclude.open("a", encoding="utf-8") as f:
                f.write("".join(f"{p}\n" for p in missing))

    def head(self) -> str:
        return git(["rev-parse", "HEAD"], self.root).strip()

    def default_branch(self) -> str:
        return git(["rev-parse", "--abbrev-ref", "HEAD"], self.root).strip()

    def path(self, rel: str | Path) -> Path:
        return safe_join(self.root, rel)

    def create_worktree(self, job_id: str, worktrees_dir: Path) -> tuple[Path, str]:
        branch = f"autospec/job-{job_id}"
        wt = worktrees_dir / f"job-{job_id}"
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            return wt, branch
        git(["worktree", "add", "-q", "-b", branch, str(wt), "HEAD"], self.root)
        return wt, branch

    def remove_worktree(self, job_id: str, worktrees_dir: Path) -> None:
        wt = worktrees_dir / f"job-{job_id}"
        if wt.exists():
            git(["worktree", "remove", "--force", str(wt)], self.root, check=False)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        git(["worktree", "prune"], self.root, check=False)
        git(["branch", "-D", f"autospec/job-{job_id}"], self.root, check=False)

    def merge(self, branch: str, message: str) -> str:
        proc = subprocess.run(
            ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
             "merge", "--no-ff", "-m", message, branch], cwd=self.root, capture_output=True, text=True)
        if proc.returncode != 0:
            git(["merge", "--abort"], self.root, check=False)
            raise GitError(f"merge failed (aborted): {proc.stdout.strip()} {proc.stderr.strip()}"[:1000])
        return self.head()


def create_workspace(root_dir: Path, name: str, mode: str = "empty", source: Path | None = None) -> Workspace:
    validate_name(name)
    dest = root_dir / name
    if dest.exists():
        raise FileExistsError(f"workspace already exists: {dest}")
    root_dir.mkdir(parents=True, exist_ok=True)
    if mode == "empty":
        ws = Workspace(name, dest)
        ws.ensure_repo(set_identity=True)
        return ws
    if mode != "import" or source is None:
        raise ValueError("import mode needs source_path")
    source = source.expanduser().resolve()
    if (source / ".git").exists():
        subprocess.run(["git", "clone", "-q", str(source), str(dest)], check=True, capture_output=True)
    else:
        shutil.copytree(source, dest, symlinks=True)
    ws = Workspace(name, dest)
    ws.ensure_repo(set_identity=True)
    if not (source / ".git").exists():
        git(["add", "-A"], dest)
        git(["commit", "-q", "-m", "autospec: import", "--allow-empty"], dest)
    return ws


def commit_paths(workdir: Path, paths: Sequence[str | Path], message: str) -> str:
    """Stage only the given paths (never `add -A` in a user's tree) and commit. Returns HEAD."""
    existing = [str(p) for p in paths if (workdir / p).exists()]
    if existing:
        git(["add", "-f", "--", *existing], workdir)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workdir).returncode != 0
    if staged:
        git(["commit", "-q", "-m", message], workdir)
    return git(["rev-parse", "HEAD"], workdir).strip()


def restore_paths(workdir: Path, sha: str, paths: Sequence[str | Path]) -> None:
    git(["checkout", sha, "--", *[str(p) for p in paths]], workdir)


def diff(workdir: Path, base: str, head: str = "HEAD") -> str:
    return git(["diff", f"{base}...{head}"], workdir)


def is_tracked_clean(workdir: Path, rel: str) -> bool:
    out = git(["status", "--porcelain", "--", rel], workdir)
    return out.strip() == ""
