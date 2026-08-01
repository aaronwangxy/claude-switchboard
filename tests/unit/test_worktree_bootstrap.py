"""Which gitignored repository files reach a new worktree.

A worktree gets exactly what Git puts there, so anything ignored is missing unless it is
copied. Copying is opt-in per file because these files are where credentials live.
"""

from __future__ import annotations

from switchboard.gitops.worktrees import WorktreeService


class TestWorktreeBootstrap:
    def test_nothing_is_copied_by_default(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        (repo / "CLAUDE.local.md").write_text("local notes")
        assert WorktreeService(tmp_path / "root").bootstrap(repo, worktree) == []
        assert not (worktree / "CLAUDE.local.md").exists()

    def test_named_files_are_copied(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        (repo / "CLAUDE.local.md").write_text("local notes")
        service = WorktreeService(tmp_path / "root", ["CLAUDE.local.md"])
        assert service.bootstrap(repo, worktree) == ["CLAUDE.local.md"]
        assert (worktree / "CLAUDE.local.md").read_text() == "local notes"

    def test_unnamed_files_are_never_swept_up(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        (repo / "CLAUDE.local.md").write_text("local notes")
        (repo / ".env").write_text("SECRET=1")
        service = WorktreeService(tmp_path / "root", ["CLAUDE.local.md"])
        service.bootstrap(repo, worktree)
        assert not (worktree / ".env").exists()

    def test_a_path_escaping_the_repository_is_refused(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        (tmp_path / "secrets.txt").write_text("SECRET=1")
        service = WorktreeService(tmp_path / "root", ["../secrets.txt"])
        assert service.bootstrap(repo, worktree) == []
        assert not (worktree / "secrets.txt").exists()
        assert not (tmp_path / "wt" / ".." / "secrets.txt").is_symlink()

    def test_a_directory_is_not_copied(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        (repo / "notes").mkdir(parents=True)
        worktree.mkdir()
        service = WorktreeService(tmp_path / "root", ["notes"])
        assert service.bootstrap(repo, worktree) == []

    def test_an_existing_file_is_not_overwritten(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        (repo / "CLAUDE.local.md").write_text("from repo")
        (worktree / "CLAUDE.local.md").write_text("already here")
        service = WorktreeService(tmp_path / "root", ["CLAUDE.local.md"])
        assert service.bootstrap(repo, worktree) == []
        assert (worktree / "CLAUDE.local.md").read_text() == "already here"

    def test_a_missing_file_is_skipped_quietly(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        service = WorktreeService(tmp_path / "root", ["CLAUDE.local.md"])
        assert service.bootstrap(repo, worktree) == []

    def test_a_symlink_pointing_out_of_the_repository_is_refused(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        repo.mkdir()
        worktree.mkdir()
        (tmp_path / "secrets.txt").write_text("SECRET=1")
        (repo / "CLAUDE.local.md").symlink_to(tmp_path / "secrets.txt")
        service = WorktreeService(tmp_path / "root", ["CLAUDE.local.md"])
        assert service.bootstrap(repo, worktree) == []
        assert not (worktree / "CLAUDE.local.md").exists()

    def test_a_file_in_a_subdirectory_keeps_its_place(self, tmp_path):
        repo, worktree = tmp_path / "repo", tmp_path / "wt"
        (repo / ".claude").mkdir(parents=True)
        worktree.mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}")
        service = WorktreeService(tmp_path / "root", [".claude/settings.local.json"])
        assert service.bootstrap(repo, worktree) == [".claude/settings.local.json"]
        assert (worktree / ".claude" / "settings.local.json").read_text() == "{}"
