"""The command surface.

Only four commands exist: two answer a question without starting the application, and
one stops it when its own UI cannot -- which is the only reason they earn their place.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager

import pytest

from switchboard.app import board_processes, main, parse_args, runtime_processes


class TestArguments:
    def test_no_command_opens_the_interface(self):
        assert parse_args([]).command is None

    def test_claude_is_the_named_form_of_the_same_thing(self):
        assert parse_args(["claude"]).command == "claude"

    def test_repositories_can_be_registered_repeatedly(self):
        args = parse_args(["--register", "/a", "--register", "/b"])
        assert args.register == ["/a", "/b"]

    def test_an_unknown_command_is_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["nonsense"])


class TestWorkflowsCommand:
    def test_it_lists_every_routable_workflow(self, sb_home, capsys):
        assert main(["workflows"]) == 0
        printed = capsys.readouterr().out
        for name in ("complete-ticket", "ask-question", "independent-review", "smoke-test"):
            assert name in printed

    def test_it_says_which_workflows_are_composite(self, sb_home, capsys):
        main(["workflows"])
        line = next(
            line for line in capsys.readouterr().out.splitlines() if line.startswith("complete-ticket")
        )
        assert "composite" in line

    def test_each_workflow_is_one_line(self, sb_home, capsys):
        """A multi-line YAML description must not break the listing into fragments."""
        main(["workflows"])
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert all(line.split()[0].replace("-", "").isalnum() for line in lines)

    def test_a_user_workflow_appears_alongside_the_builtins(self, sb_home, isolated_workflows, capsys):
        isolated_workflows.mkdir(parents=True, exist_ok=True)
        (isolated_workflows / "my-ritual.yaml").write_text(
            "name: my-ritual\ndescription: Mine.\nrole: implementer\nprompt: Do {job_title}.\n"
        )
        main(["workflows"])
        assert "my-ritual" in capsys.readouterr().out


class TestConfigCommand:
    def test_it_prints_where_everything_lives(self, sb_home, capsys):
        assert main(["config"]) == 0
        printed = capsys.readouterr().out
        for label in ("config file", "data directory", "database", "worktree root"):
            assert label in printed
        assert str(sb_home) in printed

    def test_it_prints_the_effective_configuration(self, sb_home, capsys):
        main(["config"])
        printed = capsys.readouterr().out
        assert '"default_composite_workflow": "complete-ticket"' in printed


class TestKillCommand:
    def test_an_idle_home_has_nothing_to_stop(self, sb_home, capsys):
        assert main(["kill", "-y"]) == 0
        assert "Nothing to stop." in capsys.readouterr().out

    def test_it_reports_which_home_it_is_acting_on(self, sb_home, capsys):
        """Everything it stops is scoped to one data directory, so that has to be visible."""
        main(["kill", "-y"])
        assert str(sb_home) in capsys.readouterr().out

    def test_declining_stops_nothing(self, sb_home, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        monkeypatch.setattr("switchboard.app.board_processes", lambda: [(1, "a board")])
        assert main(["kill"]) == 1
        assert "Left alone." in capsys.readouterr().out

    def test_it_finds_this_home_s_runtimes_and_leaves_another_home_s_alone(
        self, sb_home, tmp_path
    ):
        """The one mistake this command must never make: two homes look alike in `ps`."""
        ours = _settings_file(sb_home)
        theirs = _settings_file(tmp_path / "elsewhere")
        with _holding(ours) as mine, _holding(theirs) as stranger:
            running = [pid for pid, _ in runtime_processes()]
            assert mine.pid in running
            assert stranger.pid not in running

    def test_only_this_home_s_database_identifies_a_board(self, sb_home, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        for database in (sb_home / "switchboard.db", other / "switchboard.db"):
            database.touch()
        with _holding(sb_home / "switchboard.db") as mine, _holding(
            other / "switchboard.db"
        ) as stranger:
            holding = [pid for pid, _ in board_processes()]
            assert mine.pid in holding
            assert stranger.pid not in holding


def _settings_file(home):
    path = home / "runtime" / "hooks" / "native-x.settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


@contextmanager
def _holding(path):
    """A process that both names `path` in its command line and holds it open."""
    process = subprocess.Popen(["tail", "-f", str(path)], stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        yield process
    finally:
        process.terminate()
        process.wait()
