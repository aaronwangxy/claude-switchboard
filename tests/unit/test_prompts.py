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
    assert "one dependent call at a time" in prompt
    assert "Never print\ntool-call markup" in prompt
    assert "Do not narrate a planned tool call" in prompt
    assert "until the requested action exists in authoritative state" in prompt
    assert "inspect authoritative state" in prompt
    assert "resolve a user-visible repository name" in prompt
    assert "without asking\nfor it or registering it again" in prompt
    assert "When the user names a composite\nworkflow, start that composite" in prompt
    assert "When no composite workflow applies" in prompt


def test_the_policy_version_is_stable_and_recorded():
    assert PROMPT_POLICY_VERSION
