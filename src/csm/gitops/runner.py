"""Git invocation. Argument arrays only -- never a shell string built from input."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed. Carries the command, exit code, and stderr."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(
            f"git {' '.join(args)} failed with exit code {returncode}: "
            f"{self.stderr or '<no stderr>'}"
        )


@dataclass(frozen=True)
class GitResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def out(self) -> str:
        return self.stdout.strip()


def run_git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> GitResult:
    """Run `git <args>` in `cwd`.

    `args` is always passed as an argument array, so model- or user-supplied text can
    never be interpreted as shell syntax.
    """
    argv = ["git", "-C", str(cwd), *args]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is a hard requirement
        raise GitError(list(args), 127, "git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(list(args), 124, f"timed out after {timeout}s") from exc
    result = GitResult(list(args), proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return result


def is_git_repository(path: Path) -> bool:
    try:
        return run_git(path, "rev-parse", "--is-inside-work-tree").out == "true"
    except GitError:
        return False


def repo_toplevel(path: Path) -> Path:
    return Path(run_git(path, "rev-parse", "--show-toplevel").out)


def current_branch(path: Path) -> str:
    return run_git(path, "rev-parse", "--abbrev-ref", "HEAD").out


def default_branch(path: Path) -> str:
    """Best-effort default branch: origin/HEAD, else the current branch."""
    result = run_git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if result.returncode == 0 and result.out:
        return result.out.rsplit("/", 1)[-1]
    return current_branch(path)


def head_commit(path: Path) -> str:
    return run_git(path, "rev-parse", "HEAD").out


def tree_hash(path: Path, ref: str = "HEAD") -> str:
    """Tree hash of a commit. Two commits with the same tree have identical content."""
    return run_git(path, "rev-parse", f"{ref}^{{tree}}").out


def is_dirty(path: Path) -> bool:
    return bool(run_git(path, "status", "--porcelain").out)


def dirty_files(path: Path) -> list[str]:
    return [line for line in run_git(path, "status", "--porcelain").stdout.splitlines() if line]


def commits_between(path: Path, base: str, head: str = "HEAD") -> list[str]:
    result = run_git(path, "log", "--oneline", f"{base}..{head}", check=False)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def diff(path: Path, base: str, head: str = "HEAD", max_chars: int = 60_000) -> str:
    result = run_git(path, "diff", f"{base}...{head}", check=False)
    text = result.stdout
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... [diff truncated at {max_chars} characters]"
    return text


def ref_exists(path: Path, ref: str) -> bool:
    return run_git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode == 0


def worktree_list(path: Path) -> str:
    return run_git(path, "worktree", "list").stdout
