"""User configuration and application paths.

Defaults live here; `~/.config/claude-session-manager/config.yaml` overrides them.
`CSM_HOME` relocates the entire data directory (used by tests and for isolated runs).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_ENV = "CSM_CONFIG"
HOME_ENV = "CSM_HOME"
WORKFLOWS_ENV = "CSM_WORKFLOWS_DIR"


class CommunicationConfig(BaseModel):
    style: str = "concise_plain_english"
    default_verbosity: str = "concise"
    status_max_sentences: int = 2
    default_expand_details: bool = False
    plan_max_lines: int = 10
    collapse_tool_output: bool = True


class SubagentConfig(BaseModel):
    enabled: bool = True
    max_concurrent_per_worker: int = 3
    prefer_read_only: bool = True
    allow_nested: bool = False


class CommitConfig(BaseModel):
    require_plan: bool = True
    atomic_by_default: bool = True
    allow_wip_commits: bool = False
    test_before_commit: bool = True


class ModelConfig(BaseModel):
    """Model IDs are configuration, not hardcoded product knowledge.

    Defaults come from the environment (`CSM_STRONG_MODEL` / `CSM_FAST_MODEL`) and fall
    back to the SDK default (``None`` -> whatever the Claude runtime is configured to use).
    """

    manager: str | None = Field(default_factory=lambda: os.getenv("CSM_STRONG_MODEL"))
    planner: str | None = Field(default_factory=lambda: os.getenv("CSM_STRONG_MODEL"))
    implementer: str | None = Field(default_factory=lambda: os.getenv("CSM_FAST_MODEL"))
    reviewer: str | None = Field(default_factory=lambda: os.getenv("CSM_STRONG_MODEL"))
    verifier: str | None = Field(default_factory=lambda: os.getenv("CSM_FAST_MODEL"))
    general: str | None = Field(default_factory=lambda: os.getenv("CSM_FAST_MODEL"))


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


class Config(BaseModel):
    communication: CommunicationConfig = Field(default_factory=CommunicationConfig)
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    commits: CommitConfig = Field(default_factory=CommitConfig)
    models: ModelConfig = Field(default_factory=ModelConfig)
    workflows: WorkflowConfig = Field(default_factory=WorkflowConfig)
    #: Whether SDK sessions load the user's and project's Claude settings.
    #: Documented default: project instructions (CLAUDE.md) yes, user settings yes.
    setting_sources: list[str] = Field(default_factory=lambda: ["user", "project"])
    #: The composite workflow a new job follows unless the job or repository says otherwise.
    default_profile: str = "complete-ticket"

    def model_for_role(self, role: str) -> str | None:
        return getattr(self.models, role, None) or self.models.general


def home_dir() -> Path:
    override = os.getenv(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "claude-session-manager"


def database_path() -> Path:
    return home_dir() / "csm.db"


def worktree_root() -> Path:
    return home_dir() / "worktrees"


def user_workflows_dir() -> Path:
    """Where the user's own workflow definitions live.

    `~/.csm/workflows` by default; relocated with the rest of CSM's state when `CSM_HOME`
    is set, so an isolated run never reads or writes the real one.
    """
    override = os.getenv(WORKFLOWS_ENV)
    if override:
        return Path(override).expanduser()
    home = os.getenv(HOME_ENV)
    if home:
        return Path(home).expanduser() / "workflows"
    return Path.home() / ".csm" / "workflows"


def config_path() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "claude-session-manager" / "config.yaml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(data)
