"""The command surface.

Only three commands exist, and two of them answer a question without starting the
application -- which is the only reason they earn their place.
"""

from __future__ import annotations

import pytest

from switchboard.app import main, parse_args


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
        assert '"default_profile": "complete-ticket"' in printed
