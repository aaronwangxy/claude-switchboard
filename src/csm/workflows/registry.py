"""Reusable workflow blocks.

Workflows are first-class domain objects backed by prompt templates. Swapping the
template for a filesystem Claude Skill later would not change this API.
"""

from __future__ import annotations

from dataclasses import dataclass

from csm.domain.enums import ArtifactType, WorkerRole

A = ArtifactType
R = WorkerRole


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    description: str
    allowed_roles: frozenset[WorkerRole]
    required_artifacts: frozenset[ArtifactType]
    produced_artifacts: frozenset[ArtifactType]
    mutates_code: bool
    invalidates: frozenset[ArtifactType]
    default_model_role: str
    policy: str = ""
    #: Rendered with `request`, `job`, `artifacts`, `config` when the workflow is invoked.
    template: str = "{request}"
    default_role: WorkerRole = R.GENERAL


CONTRACT_JSON_INSTRUCTION = """\
End your reply with exactly one fenced ```json block. The application parses that block
deterministically -- prose outside it is for the user, the block is the artifact."""

PLAN_TEMPLATE = """\
Plan this work for the repository in your working directory.

Request:
{request}

Produce a user-facing plan of at most {plan_max_lines} short lines, then the structured
contract. Ask for a human decision only where the answer changes the result; give concrete
options and a recommendation. If any decision blocks implementation, end your prose with
[NEEDS DECISION] and one concrete question.

""" + CONTRACT_JSON_INSTRUCTION + """
Schema:
{{
  "summary_lines": ["<=10 short lines"],
  "decisions": [{{"id","question","options":[],"recommendation","blocking":true}}],
  "commit_stack": [{{"order":1,"message","purpose"}}],
  "risks": [],
  "base_commit": "<git rev-parse HEAD>",
  "criteria": [{{"id":"AC1","behavior","verification_method","evidence_required":[]}}]
}}
"criteria" are the behavior contract: externally observable behavior, not implementation
activity. Include the deepest practical end-to-end or smoke test this environment permits."""

IMPLEMENT_TEMPLATE = """\
Implement the approved plan in your working directory.

Original request:
{request}

Approved contracts and decisions:
{artifacts}

Work through the approved commit stack in order. Commit as you go. Run the relevant focused
checks before each commit. When you finish, report in one or two sentences: what changed,
the commits you created, verification status, and any difference from the approved stack."""

VERIFY_TEMPLATE = """\
Verify this change against its acceptance criteria. Scope: {scope}.

Acceptance criteria and evidence requirements:
{artifacts}

Run the checks yourself in your working directory. Record the exact commands and exit codes.
Report a criterion as passed only with evidence you actually observed; otherwise use
"failed", "not_tested", or "blocked" and state the limitation.

""" + CONTRACT_JSON_INSTRUCTION + """
Schema:
{{
  "scope": "{scope}",
  "evidence": [{{
    "criterion_id": "AC1",
    "status": "passed|failed|not_tested|blocked",
    "commands": [{{"command","exit_code","output_excerpt"}}],
    "observed_behavior": "",
    "artifacts": [],
    "limitations": []
  }}]
}}"""

REVIEW_TEMPLATE = """\
Review this change independently. You are seeing it fresh.

Original request:
{request}

Approved contracts, decisions, and verification evidence:
{artifacts}

Commit range: {base_commit}..{head_commit}
Commits:
{commits}

Diff:
{diff}

Assess implementation correctness, whether the acceptance criteria were met, whether the
plan or criteria missed important behavior, and architecture, security, maintainability,
and commit quality. Verdict first, then only actionable findings ordered by severity.

""" + CONTRACT_JSON_INSTRUCTION + """
Schema:
{{
  "verdict": "pass|changes_requested",
  "findings": [{{
    "id":"F1","severity":"blocking|important|minor|nit","category":"",
    "description":"","evidence":"","recommended_action":""
  }}]
}}"""

REVIEW_COMMENTS_TEMPLATE = """\
Address these review comments in your working directory.

{request}

Job context:
{artifacts}

For each comment: inspect the claim and the relevant code, classify it, then fix valid
issues or give a concise evidence-based reason for no change. Commit fixes.

""" + CONTRACT_JSON_INSTRUCTION + """
Schema:
{{
  "resolutions": [{{
    "original_comment":"",
    "classification":"valid|partially_valid|invalid|already_addressed|needs_human_decision",
    "reasoning":"","action_taken":null,"commit":null,"verification_required":[]
  }}]
}}"""

REBASE_TEMPLATE = """\
Rebase this stack onto {base_ref} in your working directory.

{request}

Configured preferences: preserve_merges={preserve_merges},
autosquash_fixups={autosquash_fixups}, never_force_push={never_force_push}.

Show the base, the commit stack before and after, any conflicts, and the result. Do not
force-push, and do not delete or reset any branch. If conflicts need judgment you cannot
make safely, stop and end your reply with [NEEDS INPUT] and one concrete question."""

RESTACK_TEMPLATE = """\
Reorganise the commit stack in your working directory without changing the working tree.

{request}

Approved commit stack:
{artifacts}

Reorder, squash, or reword commits only. The final tree must be byte-identical to the
current tree -- verify with `git rev-parse HEAD^{{tree}}` before and after. Never force-push."""

QUESTION_TEMPLATE = """\
Answer this question about the codebase in your working directory. You are read-only.

{request}

Answer directly first. Add explanation only where it changes the answer."""

FINALIZE_TEMPLATE = """\
Summarise this change for handoff.

Job context and stored evidence:
{artifacts}

Report the branch, the commit stack, working-tree status, a verification summary, a review
summary, and honest limitations. Do not push, merge, or delete anything."""


WORKFLOWS: dict[str, WorkflowDefinition] = {
    "plan-feature": WorkflowDefinition(
        name="plan-feature",
        description="Produce implementation, behavior, and evidence contracts for a request.",
        allowed_roles=frozenset({R.PLANNER}),
        required_artifacts=frozenset(),
        produced_artifacts=frozenset({A.IMPLEMENTATION_CONTRACT, A.BEHAVIOR_CONTRACT}),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="planner",
        default_role=R.PLANNER,
        template=PLAN_TEMPLATE,
    ),
    "implement-approved-plan": WorkflowDefinition(
        name="implement-approved-plan",
        description="Implement an approved plan as an atomic commit stack.",
        allowed_roles=frozenset({R.IMPLEMENTER}),
        required_artifacts=frozenset({A.IMPLEMENTATION_CONTRACT}),
        produced_artifacts=frozenset(),
        mutates_code=True,
        invalidates=frozenset({A.VERIFICATION, A.SMOKE_VERIFICATION, A.REVIEW}),
        default_model_role="implementer",
        default_role=R.IMPLEMENTER,
        template=IMPLEMENT_TEMPLATE,
    ),
    "smoke-test": WorkflowDefinition(
        name="smoke-test",
        description="Targeted rerun of the deepest practical end-to-end check.",
        allowed_roles=frozenset({R.VERIFIER}),
        required_artifacts=frozenset({A.BEHAVIOR_CONTRACT}),
        produced_artifacts=frozenset({A.SMOKE_VERIFICATION}),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="verifier",
        default_role=R.VERIFIER,
        template=VERIFY_TEMPLATE,
    ),
    "full-verify": WorkflowDefinition(
        name="full-verify",
        description="Verify every acceptance criterion against current HEAD.",
        allowed_roles=frozenset({R.VERIFIER}),
        required_artifacts=frozenset({A.BEHAVIOR_CONTRACT}),
        produced_artifacts=frozenset({A.VERIFICATION}),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="verifier",
        default_role=R.VERIFIER,
        template=VERIFY_TEMPLATE,
    ),
    "review-change": WorkflowDefinition(
        name="review-change",
        description="Fresh independent review of the commit range against the contracts.",
        allowed_roles=frozenset({R.REVIEWER}),
        required_artifacts=frozenset({A.IMPLEMENTATION_CONTRACT}),
        produced_artifacts=frozenset({A.REVIEW}),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="reviewer",
        default_role=R.REVIEWER,
        template=REVIEW_TEMPLATE,
    ),
    "rereview": WorkflowDefinition(
        name="rereview",
        description="Start a brand new reviewer against current HEAD.",
        allowed_roles=frozenset({R.REVIEWER}),
        required_artifacts=frozenset({A.IMPLEMENTATION_CONTRACT}),
        produced_artifacts=frozenset({A.REVIEW}),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="reviewer",
        default_role=R.REVIEWER,
        template=REVIEW_TEMPLATE,
    ),
    "address-review-comments": WorkflowDefinition(
        name="address-review-comments",
        description="Classify and resolve each review comment.",
        allowed_roles=frozenset({R.IMPLEMENTER, R.REVIEW_COMMENTS}),
        required_artifacts=frozenset(),
        produced_artifacts=frozenset({A.COMMENT_RESOLUTIONS}),
        mutates_code=True,
        invalidates=frozenset({A.VERIFICATION, A.SMOKE_VERIFICATION, A.REVIEW}),
        default_model_role="implementer",
        default_role=R.IMPLEMENTER,
        template=REVIEW_COMMENTS_TEMPLATE,
    ),
    "rebase-stack": WorkflowDefinition(
        name="rebase-stack",
        description="Rebase the job's commit stack onto its base ref.",
        allowed_roles=frozenset({R.IMPLEMENTER, R.REBASE}),
        required_artifacts=frozenset(),
        produced_artifacts=frozenset(),
        mutates_code=True,
        invalidates=frozenset({A.VERIFICATION, A.SMOKE_VERIFICATION, A.REVIEW}),
        default_model_role="implementer",
        default_role=R.IMPLEMENTER,
        template=REBASE_TEMPLATE,
    ),
    "restack-commits": WorkflowDefinition(
        name="restack-commits",
        description="Reorder or squash commits without changing the tree.",
        allowed_roles=frozenset({R.IMPLEMENTER, R.REBASE}),
        required_artifacts=frozenset(),
        produced_artifacts=frozenset(),
        mutates_code=True,
        invalidates=frozenset(),
        default_model_role="implementer",
        default_role=R.IMPLEMENTER,
        template=RESTACK_TEMPLATE,
    ),
    "answer-codebase-question": WorkflowDefinition(
        name="answer-codebase-question",
        description="Answer a question about the codebase read-only.",
        allowed_roles=frozenset({R.QUESTION, R.GENERAL, R.IMPLEMENTER, R.PLANNER}),
        required_artifacts=frozenset(),
        produced_artifacts=frozenset(),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="general",
        default_role=R.QUESTION,
        template=QUESTION_TEMPLATE,
    ),
    "finalize-change": WorkflowDefinition(
        name="finalize-change",
        description="Summarise branch, commits, evidence, and limitations for handoff.",
        allowed_roles=frozenset({R.IMPLEMENTER, R.GENERAL}),
        required_artifacts=frozenset(),
        produced_artifacts=frozenset(),
        mutates_code=False,
        invalidates=frozenset(),
        default_model_role="general",
        default_role=R.IMPLEMENTER,
        template=FINALIZE_TEMPLATE,
    ),
}


class WorkflowError(ValueError):
    """A workflow was requested that does not exist or is not allowed for the role."""


def get_workflow(name: str) -> WorkflowDefinition:
    try:
        return WORKFLOWS[name]
    except KeyError:
        raise WorkflowError(
            f"Unknown workflow {name!r}. Known workflows: {', '.join(sorted(WORKFLOWS))}."
        ) from None


def validate_for_role(name: str, role: WorkerRole) -> WorkflowDefinition:
    definition = get_workflow(name)
    if role not in definition.allowed_roles:
        raise WorkflowError(
            f"Workflow {name!r} cannot run on a {role.value} worker; "
            f"allowed roles: {', '.join(sorted(r.value for r in definition.allowed_roles))}."
        )
    return definition


#: The default bundle for a normal feature ticket.
FEATURE_BUNDLE = [
    "plan-feature",
    "implement-approved-plan",
    "full-verify",
    "review-change",
]
