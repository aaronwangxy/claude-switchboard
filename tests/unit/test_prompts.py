"""Prompt composition: concision is appended to the coding-agent preset, never instead of it."""

from __future__ import annotations

import pytest

from switchboard.agents.prompts import (
    CONCISION_POLICY,
    MANAGER_POLICY,
    PROMPT_POLICY_VERSION,
    ROLE_POLICIES,
    compose_manager_prompt,
    compose_worker_prompt,
)
from switchboard.agents.sdk_backend import READ_ONLY_TOOLS, WRITE_TOOLS, SdkWorkerBackend
from switchboard.config import Config
from switchboard.domain.enums import Verbosity, WorkerRole


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.mark.parametrize("role", list(WorkerRole))
def test_every_role_gets_the_global_concision_policy(config, role):
    prompt = compose_worker_prompt(role, config, writable=True)
    assert CONCISION_POLICY in prompt


@pytest.mark.parametrize("role", list(WorkerRole))
def test_no_prompt_ever_restricts_reasoning_or_tools(config, role):
    prompt = compose_worker_prompt(role, config, writable=True)
    assert "Think and investigate as deeply as" in prompt
    assert "Never omit a blocker" in prompt


def test_role_policies_are_role_specific(config):
    planner = compose_worker_prompt(WorkerRole.PLANNER, config, writable=False)
    reviewer = compose_worker_prompt(WorkerRole.REVIEWER, config, writable=False)
    assert "at most 10 short lines" in planner
    assert "Verdict first" in reviewer
    assert ROLE_POLICIES[WorkerRole.PLANNER] not in reviewer


def test_the_reviewer_is_told_it_has_no_implementer_reasoning(config):
    prompt = compose_worker_prompt(WorkerRole.REVIEWER, config, writable=False)
    assert "you do not have" in prompt
    assert "implementer's reasoning" in prompt


def test_read_only_workers_are_told_so_and_writable_ones_are_not(config):
    read_only = compose_worker_prompt(WorkerRole.QUESTION, config, writable=False)
    writable = compose_worker_prompt(WorkerRole.IMPLEMENTER, config, writable=True)
    assert "running read-only" in read_only
    assert "running read-only" not in writable


def test_the_subagent_budget_comes_from_configuration(config):
    config.subagents.max_concurrent_per_worker = 2
    prompt = compose_worker_prompt(WorkerRole.IMPLEMENTER, config, writable=True)
    assert "at most 2 helpers" in prompt
    assert "no nested fan-out" in prompt

    config.subagents.enabled = False
    assert "helpers at" not in compose_worker_prompt(
        WorkerRole.IMPLEMENTER, config, writable=True
    )


def test_read_only_workers_are_not_given_a_subagent_budget(config):
    assert "helpers at" not in compose_worker_prompt(WorkerRole.REVIEWER, config, writable=False)


def test_verbosity_changes_presentation_only(config):
    detailed = compose_worker_prompt(
        WorkerRole.GENERAL, config, writable=True, verbosity=Verbosity.DETAILED
    )
    assert "presentation only" in detailed
    assert CONCISION_POLICY in detailed, "the base policy is still present"
    assert compose_worker_prompt(WorkerRole.GENERAL, config, writable=True).count("Verbosity:") == 0


def test_manager_prompt_carries_its_own_response_policy():
    prompt = compose_manager_prompt()
    assert MANAGER_POLICY in prompt
    assert "you do not write code" in prompt
    assert "NOT the system of" in prompt  # the phrase wraps a line in the template


def test_the_policy_version_is_stable_and_recorded():
    assert PROMPT_POLICY_VERSION


# ------------------------------------------------------------- tool policy


def test_read_only_workers_get_no_file_editing_tools():
    """Read-only is enforced by tool policy, not a sandbox.

    Every dedicated file-editing tool is withheld, but Bash remains so verifiers and
    reviewers can run git and the test suite -- so this pins the exact allowed set
    rather than claiming the worker cannot write at all.
    """
    backend = SdkWorkerBackend()
    spec = _spec(writable=False)
    options = backend._options(spec)
    assert set(WRITE_TOOLS) <= set(options.disallowed_tools)
    assert set(options.allowed_tools) == set(READ_ONLY_TOOLS)
    assert not set(WRITE_TOOLS) & set(options.allowed_tools)
    assert "Bash" in options.allowed_tools, "documented, deliberate exception"


def test_no_worker_is_given_manager_tools_or_registry_access():
    backend = SdkWorkerBackend()
    for writable in (True, False):
        options = backend._options(_spec(writable=writable))
        assert options.mcp_servers == {}, "workers get no in-process tool server"


def test_workers_use_the_coding_agent_preset_rather_than_a_custom_chatbot_prompt():
    options = SdkWorkerBackend()._options(_spec(writable=True))
    assert options.system_prompt["preset"] == "claude_code"
    assert CONCISION_POLICY in options.system_prompt["append"]


def _spec(writable: bool):
    from pathlib import Path
    from uuid import uuid4

    from switchboard.agents.backend import WorkerSpec

    role = WorkerRole.IMPLEMENTER if writable else WorkerRole.REVIEWER
    return WorkerSpec(
        worker_id=uuid4(),
        role=role.value,
        cwd=Path("/tmp/repo"),
        system_prompt_append=compose_worker_prompt(role, Config(), writable=writable),
        initial_prompt="go",
        writable=writable,
    )
