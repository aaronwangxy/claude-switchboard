"""User configuration and application paths.

Defaults live here; `~/.config/switchboard/config.yaml` overrides them.
`SB_HOME` relocates the entire data directory (used by tests and for isolated runs).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import AliasChoices, BaseModel, Field

CONFIG_ENV = "SB_CONFIG"
HOME_ENV = "SB_HOME"
WORKFLOWS_ENV = "SB_WORKFLOWS_DIR"


class SubagentConfig(BaseModel):
    """Whether workers may spawn Claude's own bounded helper subagents, and how many."""

    enabled: bool = True
    max_concurrent_per_worker: int = 3


class CommitConfig(BaseModel):
    #: Implementation may not start without an approved implementation contract.
    require_plan: bool = True


class ModelConfig(BaseModel):
    """Model IDs are configuration, not hardcoded product knowledge.

    Defaults come from the environment (`SB_STRONG_MODEL` / `SB_FAST_MODEL`) and fall
    back to the native Claude default (``None`` -> the configured runtime's default model).

    Extra keys are allowed so a role a workflow invented -- `investigator`, say -- can be
    configured here without Switchboard having to know the name in advance.
    """

    model_config = {"extra": "allow"}

    manager: str | None = Field(default_factory=lambda: os.getenv("SB_STRONG_MODEL"))
    planner: str | None = Field(default_factory=lambda: os.getenv("SB_STRONG_MODEL"))
    implementer: str | None = Field(default_factory=lambda: os.getenv("SB_FAST_MODEL"))
    reviewer: str | None = Field(default_factory=lambda: os.getenv("SB_STRONG_MODEL"))
    verifier: str | None = Field(default_factory=lambda: os.getenv("SB_FAST_MODEL"))
    general: str | None = Field(default_factory=lambda: os.getenv("SB_FAST_MODEL"))


class RebaseWorkflowConfig(BaseModel):
    preserve_merges: bool = False
    autosquash_fixups: bool = True
    never_force_push: bool = True


class PlanWorkflowConfig(BaseModel):
    max_plan_lines: int = 10


class ReviewWorkflowConfig(BaseModel):
    blocking_severities: list[str] = Field(default_factory=lambda: ["blocking", "important"])


class WorkflowConfig(BaseModel):
    rebase_stack: RebaseWorkflowConfig = Field(default_factory=RebaseWorkflowConfig, alias="rebase-stack")
    plan_feature: PlanWorkflowConfig = Field(default_factory=PlanWorkflowConfig, alias="plan-feature")
    review_change: ReviewWorkflowConfig = Field(default_factory=ReviewWorkflowConfig, alias="review-change")

    model_config = {"populate_by_name": True}


class ClaudeConfig(BaseModel):
    """How to reach the Claude runtime.

    The executable is configuration rather than a hardcoded `claude`, so a wrapper can be
    used instead. The parent environment is always inherited so such a wrapper keeps
    working; `env` only adds to it. Nothing here can bypass managed policy -- the wrapper
    is still the Claude CLI, and Switchboard only chooses which one to launch.
    """

    executable: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class WorktreeBootstrapConfig(BaseModel):
    """Gitignored repository-local files to copy into a new worktree.

    Empty by default: a worktree gets exactly what Git puts there. Only files named here
    are copied, and only from the repository root, so nothing sweeps up `.env` or a
    credential file by accident.
    """

    files: list[str] = Field(default_factory=list)


class Config(BaseModel):
    model_config = {"populate_by_name": True}

    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    commits: CommitConfig = Field(default_factory=CommitConfig)
    models: ModelConfig = Field(default_factory=ModelConfig)
    workflows: WorkflowConfig = Field(default_factory=WorkflowConfig)
    #: The composite workflow a new job follows unless the job or repository says
    #: otherwise. `default_profile` is the key this had before Phase 10.
    default_composite_workflow: str = Field(
        default="complete-ticket",
        validation_alias=AliasChoices("default_composite_workflow", "default_profile"),
    )
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    worktree_bootstrap: WorktreeBootstrapConfig = Field(default_factory=WorktreeBootstrapConfig)

    def model_for_role(self, role: str) -> str | None:
        return getattr(self.models, role, None) or self.models.general


def home_dir() -> Path:
    override = os.getenv(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "switchboard"


def database_path() -> Path:
    return home_dir() / "switchboard.db"


def worktree_root() -> Path:
    return home_dir() / "worktrees"


def user_workflows_dir() -> Path:
    """Where the user's own workflow definitions live.

    `~/.switchboard/workflows` by default; relocated with the rest of Switchboard's
    state when `SB_HOME` is set, so an isolated run never reads or writes the real one.
    """
    override = os.getenv(WORKFLOWS_ENV)
    if override:
        return Path(override).expanduser()
    home = os.getenv(HOME_ENV)
    if home:
        return Path(home).expanduser() / "workflows"
    return Path.home() / ".switchboard" / "workflows"


def config_path() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "switchboard" / "config.yaml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(data)
