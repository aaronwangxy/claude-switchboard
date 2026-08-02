# Workflow harness milestone — verification record

> **Historical evidence.** This records the earlier SDK milestone and is not current
> architecture or usage guidance. Production Manager and workers are persistent native Claude
> processes; entry attaches to the exact live tmux target and never uses `--resume`. See
> [`native-workers.md`](native-workers.md) and [`phase6-native-manager.md`](phase6-native-manager.md).

What was claimed, and what was actually run to check it. Everything here corresponds to
`b49156a..HEAD`. The MVP-era record is in [`mvp-evidence.md`](mvp-evidence.md).

## The decision that shaped this milestone

The plan was to build a workflow harness. Partway through, the question became whether
current Claude Code already provides it. It does provide a great deal, and the design
changed accordingly: Switchboard delegates the agent loop, tools, session persistence and resume,
subagents, skills, and settings inheritance, and keeps only the layer above.

**Dynamic Workflows were evaluated as a replacement for Switchboard's composite runs and
rejected on three documented limits** (Claude Code 2.1.220, `code.claude.com/docs/en/workflows`):

| Requirement | Dynamic Workflows |
| --- | --- |
| Human sign-off between stages | "No mid-run user input… For sign-off between stages, run each stage as its own workflow" |
| Survives restarting Switchboard | "Resume works within the same Claude Code session. If you exit Claude Code while a workflow is running, the next session starts the workflow fresh" |
| Units the user can enter and drive | Its `agent()` units are subagents in an isolated runtime |

Switchboard's runs need all three. Dynamic Workflows remain a good tool *inside* a worker, and
workers can already reach for them.

The same investigation found the opposite result for attach, which is why that feature
exists: Switchboard's workers were already ordinary sessions on disk, and Switchboard had simply never
exposed it.

## Attach, verified against the real runtime

Not scripted, not mocked — a real worker through `SdkWorkerBackend`, then a real
`claude --resume` from the command line:

This run predates the rename to Switchboard, so the transcript below is left verbatim
and still shows the old `csm` names.

```
1. starting a real worker through CSM's SDK backend ...
   worker replied: Acknowledged: magenta-pelican-4417.
   session id: afe48455-b7fb-49c9-86af-8ba5cc73cfe5
2. the session is on disk where the Claude runtime keeps every session ...
   get_session_info -> found  cwd=/private/var/.../csm-smoke-99tgxnf9/wt  branch=csm/smoke
   list_sessions    -> contains it
3. CSM builds the attach command ...
   cd /private/var/.../csm-smoke-99tgxnf9/wt && claude --resume afe48455-b7fb-49c9-86af-8ba5cc73cfe5
4. resuming that session from the command line, as the user would ...
   resumed session answered: magenta-pelican-4417

RESULT: PASS - the resumed session remembers the worker's context
```

A codeword given to the Switchboard worker came back out of the resumed session. That is the
whole claim: a worker is an ordinary Claude session, and entering it needs no bridge.

## Routing, driven through the real manager

`DeterministicManager` against a real repository on the scripted backend. No workflow was
named by hand; the text alone chose each one.

| Request | Route | Worker |
| --- | --- | --- |
| a pasted ticket | `complete-ticket` | read-only planner, no worktree |
| — after answering the decision and approving the plan | `implement-approved-plan` | writable implementer in `…/worktrees/repo/eng-500-implementer-a30a42e5` |
| "Rebase ENG-500 onto main." | `rebase-stack` | the job's existing implementer |
| "Run another smoke test on ENG-500." | `smoke-test` | verifier |
| "Why does this repository use Redis?" (nothing selected) | `ask-question` | fresh read-only question worker, no worktree |
| "Let me into ENG-500." | attach | the implementer's own session |

The writable worker's path is under the managed root and outside the user's checkout,
asserted rather than eyeballed.

A question asked *while a job is selected* deliberately reuses the worker that already has
the context rather than starting a fresh reader; `ask-question` declares `implementer` in
its `allowed_roles` for that reason and does not mutate code.

## Fixes that came from independent review

A fresh reviewer that had not seen the implementation found one blocking issue and six
important ones. All were fixed and re-verified; the two that mattered most:

- **A repository's `.switchboard/workflows` could redefine a built-in.** Every field defaults to
  permissive, so a file that merely reused a name stripped `requires` and `mutates_code` —
  from inside the repository those exist to constrain, for *every* registered repository,
  since the registry is global. Built-in names are now reserved (invariant 10).
- **Attach claimed more than it did and took away more than it gave.** `send` still worked
  during an attach, so Switchboard could append to a session file the user's own client was
  writing; and the paused run had no reachable resume path, so pressing `Ctrl+E` ended the
  ritual permanently. Both fixed (invariant 11, and `resume_run` as a tool and a route).

## Checks

At `HEAD`: `298 passed`, `ruff` clean, `mypy` clean over 39 files. The suite was run four
consecutive times while chasing an intermittent `NoMatches` repaint failure, which was a
real robustness bug and is fixed.

`docs/ui-*.txt` are regenerated by `scripts/capture_ui.py` rather than pasted, and every
line quoted from them in `mvp-evidence.md` §9.8 was checked back against the file.

## Not verified

- The `claude.executable` wrapper path is tested for resolution and refusal, and the value
  is asserted to reach `ClaudeAgentOptions.cli_path`. No actual third-party wrapper was
  run, because none was available.
- `mine-workflows` is verified end to end from a synthetic proposal through acceptance to a
  routable workflow. No real mining run against a long history has happened yet — there is
  not enough history on this installation for one to be meaningful.
- Attaching was smoke-tested through `claude --resume` directly. The `Ctrl+E` path also
  suspends the Textual app, which cannot be exercised headlessly.
