# Discord plan-to-PR automation MVP

This repo now has a Discord-facing intake layer on top of the durable automation scaffold.

## Channels

The current Discord routing is stored in `.automation/discord-routing.json`.

- `build-control` — operator drops new plan requests here.
- `hermes-general` — normal Hermes chat / general inquiries.

The build-control trigger is:

```text
build plan: <plan text>
```

Example:

```text
build plan: # Add billing settings
- Add a settings page for billing email
- Persist billing email in the user profile
- Add tests for validation
```

The poller accepts messages only from the configured operator user ID.

## What happens on intake

`build-control` supports two intake paths:

1. `scripts/discord-plan-poller.py` polls the build-control channel. For every new operator message containing `build plan:` it:
   1. extracts the plan text;
   2. creates one Discord thread for the plan;
   3. writes durable artifacts under `plans/<plan-id>/`;
   4. updates `.automation/plans-index.json`;
   5. posts a short accepted/status message in build-control.
2. `scripts/ingest-source-plan.py` ingests an existing markdown plan file. It first writes a source-author audit, source-status audit, and a 5x5 ingest audit under `plans/<plan-id>/`; plans are fail-closed unless they are operator-authored (`From: Dom`, `Author: Dom`, or configured alias) or the operator records `--force-author-override`. Retired/superseded/blocked source plans also fail closed before decomposition unless the operator passes `--force-status-override`.

The first durable state is `CONTRACT`; execution does not begin until the plan is shaped into a contract.

For an existing plan file:

```bash
python3 scripts/ingest-source-plan.py \
  --plan-file /path/to/target-repo/docs/plans/example.md \
  --repo /path/to/target-repo \
  --base-branch main \
  --thread-id existing-thread-id \
  --no-discord
```

To let a vetted active plan proceed through decomposition and dispatch in one operator-run command, add the explicit gates:

```bash
python3 scripts/ingest-source-plan.py \
  --plan-file /path/to/target-repo/docs/plans/example.md \
  --repo /path/to/target-repo \
  --base-branch main \
  --thread-id existing-thread-id \
  --no-discord \
  --auto-approve \
  --decompose \
  --dispatch \
  --execute-dispatch
```

This only prepares the isolated worktree and `builder-prompt.md`; it does not invent code changes or publish a PR. `run-builder-worker.py` and `publish-draft-pr.py` still own those gates.

For status/design plans that reference an open PR stack or a deferred decision, decomposition routes to PR-status / PR-maintenance / decision packets instead of turning every markdown bullet into a fake build. Before any in-flight build/maintenance/decision proceeds, create its dedicated Discord thread:

```bash
python3 scripts/ensure-build-threads.py
```

Use `--dry-run` to preview missing task threads without creating Discord threads. Thread openers are persistent task cards: they include what the task is, where it is, what must happen to complete it, and the decision question when the task is waiting on the operator. The worker also adds the configured operator as a thread member when Discord permits it.

## Contract shaping

Run:

```bash
python3 scripts/shape-plan-contract.py --plan-id <plan-id>
```

If the plan is ambiguous, the script writes `questions.md`, sets the plan state to `QUESTION`, and sets `awaiting_operator: true`.

If the contract is concrete, the script stops at `CONTRACT_REVIEW`; concrete text is not treated as approval. Approve or reject the contract explicitly:

```bash
python3 scripts/approve-plan-contract.py --plan-id <plan-id> --decision APPROVE
```

For a controlled/manual approval during development only:

```bash
python3 scripts/shape-plan-contract.py --plan-id <plan-id> --auto-approve
```

## PR decomposition

After the contract is ready:

```bash
python3 scripts/decompose-plan-to-prs.py --plan-id <plan-id>
```

This creates PR-sized task packets under `tasks/` and writes `plans/<plan-id>/prs.json` plus `decomposition.md`.

## Cron

A Hermes cron job named `bk-discord-plan-poller` runs every minute. The wrapper at `~/.hermes/scripts/bk-discord-plan-poller.py` stays silent unless it accepts a new plan, so it should not spam local delivery.

## Current boundary

The intake/contract/decomposition layers are implemented. Execution is now split into two deterministic workers:

1. `scripts/dispatch-pr-worker.py` selects one eligible task packet, validates that the target repo is a concrete local git checkout, prepares an isolated git worktree/branch, writes `builder-prompt.md`, `summary.md`, and `evidence.md`, and queues an initial SHA-scoped PR-readiness gate for the base dispatch SHA.
2. `scripts/run-builder-worker.py` consumes the dispatched packet, runs an operator/configured builder command inside the isolated worktree, records command output and build evidence, commits produced changes, and queues the SHA-scoped PR-readiness gate for the new commit.

3. `scripts/publish-draft-pr.py` publishes a draft GitHub PR only after the task's recorded readiness job passes for the worktree's current HEAD SHA. Without `--execute`, it dry-runs and reports `WOULD_PUBLISH`.

The workers do **not** invent code changes by themselves and do not create empty PRs. Draft PR creation happens only after a builder command has produced commits and the readiness gate passes for that exact commit.

For a safe dispatch dry run:

```bash
python3 scripts/dispatch-pr-worker.py
```

To prepare the isolated worktree for the next eligible packet:

```bash
python3 scripts/dispatch-pr-worker.py --execute
```

To run a builder command against the dispatched worktree:

```bash
python3 scripts/run-builder-worker.py \
  --task-id <task-id> \
  --builder-command '<command that reads $BUILDER_PROMPT_PATH and edits the worktree>'
```

The builder command receives these environment variables:

- `BUILD_TASK_ID`
- `BUILD_TASK_DIR`
- `BUILDER_PROMPT_PATH`
- `BUILDER_SUMMARY_PATH`
- `BUILDER_EVIDENCE_PATH`

If no command is passed, the worker reads `HERMES_BUILDER_COMMAND`.

To dry-run draft PR publishing after readiness passes:

```bash
python3 scripts/publish-draft-pr.py --task-id <task-id>
```

To actually push the task branch and create the draft PR:

```bash
python3 scripts/publish-draft-pr.py --task-id <task-id> --execute
```

The publisher refuses to push or call `gh pr create` if the readiness job is stale, missing, or failed for the current worktree SHA.

## PR readiness and status channel

See `docs/pr-readiness-and-status-channel.md` for the SHA-scoped readiness gate and PR status channel monitor.

The readiness gate is event-based: call `scripts/pr-readiness-gate.py` when a branch is about to be claimed PR-ready. It certifies one commit SHA and blocks only ready-for-review / merge-ready claims for stale or failed audits; it does not block continued building.

The PR status monitor is safe-by-default: `scripts/github-pr-status-monitor.py` updates one Discord message per open operator-authored PR and opens/pings an issue thread when reviews, comments, failing checks, or rebase states need attention.

Because the PR status monitor intentionally searches only open PRs, `scripts/reconcile-merged-prs.py` closes the loop after merge: it scans task packets with `pr_packet.pr_number`, queries those PRs by number, marks merged PR tasks `DONE`, and sets `dependencies_cleared` on dependent decision/build packets. The cron wrapper is `bk-merged-pr-reconciler.sh`.

`scripts/reconcile-plan-progress.py` closes the parent-plan lifecycle loop: it scans child tasks by `source_plan_id`, promotes answered decision packets to dispatch-ready, updates parent plan state/reason from active child progress, and marks the parent `DONE` once every child task is terminal. The cron wrapper is `bk-plan-progress-reconciler.sh`.

## Open plan router and thread replies

`scripts/open-plan-router.py` classifies each open plan into the next safe handler/action. It makes stalled-looking `CONTRACT` plans explicit (`shape_contract`, `agent_required`) and routes `EXECUTING` plans to `scripts/dispatch-pr-worker.py` when the dispatcher exists.

`scripts/open-plan-status-monitor.py` keeps each active plan thread grounded with a persistent plan card. The card is posted once and then edited as state changes; it summarizes the plan, where it is, what must happen to complete it, and any decision/reply needed. It also adds the operator as a thread member when possible so bot-created threads are visible in Discord.

`scripts/discord-plan-thread-poller.py` polls active plan threads for operator replies. Replies in `QUESTION` move the plan back to `CONTRACT`; plain-text `approve` (no slash) / `reject` / `cancel` replies in `CONTRACT_REVIEW` approve/reject/cancel the contract. The same poller also watches child task/decision threads: an operator reply is recorded into task metadata, clears `awaiting_operator`, and moves decision tasks out of `QUESTION` so build-control can continue routing. Do not use `/approve` for plan approval: Discord routes that to Hermes dangerous-command approval, not build-control, and build-control channels may be ignored by the Hermes gateway.

## Open plan status monitor

`scripts/open-plan-status-monitor.py` applies the same "alert once, then suppress" contract to build-control plans:

- active plans in `CONTRACT`, `DECOMPOSE`, `EXECUTING`, or verification-style states update durable status but do not ping repeatedly;
- plans in `QUESTION` or with `awaiting_operator: true` ping the configured operator once in the plan thread and then suppress duplicates while the same operator action is pending;
- when a plan reaches `DONE` or `CANCELLED`, the monitor posts a terminal summary and archives/locks the plan thread so stale plan threads close automatically.

The monitor writes `.automation/plan-status-ledger.json` and `.automation/status/open-plan-status-last.json`.
