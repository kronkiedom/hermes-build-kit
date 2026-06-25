# PR readiness gate and status channel

This layer adds two event-shaped controls to the portable build kit:

1. **PR-readiness gate** — an asynchronous certification step over one immutable commit SHA.
2. **PR status channel monitor** — one Discord status message per open operator-authored PR, with issue threads for actionable feedback.

## PR-readiness gate

Use the gate before claiming that code is ready for GitHub PR review.

```bash
python3 scripts/pr-readiness-gate.py queue \
  --task-id <task-id> \
  --branch <branch> \
  --sha <commit-sha> \
  --pr-url <optional-pr-url>
```

The job is written under `.automation/pr-readiness/` and records:

- the audited SHA;
- the branch and task ID;
- `blocks`: `ready_for_review`, `merge_ready`, and `pr_ready_claim`;
- `does_not_block`: `continue_building`, `draft_pr`, `other_packets`, and `fix_issues`.

After the 5x5 two-loop audit completes, record the result:

```bash
python3 scripts/pr-readiness-gate.py result \
  --job-id <job-id> \
  --passed \
  --issues-json '[]'
```

For review-cleanup / re-review claims, include structured closure evidence so the gate can fail closed on missing adversarial coverage:

```bash
python3 scripts/pr-readiness-gate.py result \
  --job-id <job-id> \
  --passed \
  --issues-json '[]' \
  --evidence-json '{"review_cleanup":{"critics":["grounding","security","regression","edge_case_matrix","fresh_review_delta"],"findings":[{"id":"review-item-1","status":"resolved","fix_commit":"abc123","evidence":"file:line quote","tests":["command excerpt"],"tags":["ssrf"],"edge_cases":["ipv6_loopback","ipv4_mapped_metadata","private_ipv4"]}]}}'
```

The review-cleanup evidence gate is intentionally stricter than a normal build pass:

- each reviewer finding needs status, fix commit, source evidence, and tests;
- cleanup needs grounding, security, regression, edge-case, and fresh-review-delta critics;
- SSRF fixes must cover IPv6 literals, IPv4-mapped metadata, and private IPv4 cases, or explicitly defer DNS-resolution pinning with a reason;
- race/state-write fixes must cover stale actor windows, prior-state guard, identity pin, and stale-binding reset.

Before posting a `Ready for re-review` comment, use the gated signal command rather than `gh pr comment` directly:

```bash
python3 scripts/pr-ready-for-rereview.py \
  --pr <pr-number> \
  --job-id <passed-readiness-job-id> \
  --body-file /tmp/ready-for-rereview.md \
  --reviewer Drake-Armor
```

This command refuses to post if the readiness job is stale or failed, so rejected PRs cannot be resubmitted for re-review without the 5x5 cleanup evidence passing on the current head SHA.

Before moving a PR to ready-for-review, check the current SHA:

```bash
python3 scripts/pr-readiness-gate.py check --job-id <job-id>
```

If the branch HEAD changed after the audit, the check returns `reason: stale_sha`; rerun the audit. This lets building continue while preventing stale audit results from certifying new code.

## Required audit contract

A passing readiness job means the specific SHA passed the 5x5 two-loop audit:

1. security audit;
2. bug/regression audit;
3. build/type/test audit;
4. no-facade/integration audit;
5. scope/overreach/maintainability audit.

Security and bug/regression are mandatory every time.

## Discord PR status channel

Create or verify the channel from the local routing config:

```bash
python3 scripts/setup-discord-pr-status-channel.py --name pr-status
```

This updates `.automation/discord-routing.json` with:

```json
{
  "pr_status_channel_id": "...",
  "pr_status_channel_name": "pr-status"
}
```

Then scan open PRs authored by the operator and sync the channel:

```bash
python3 scripts/github-pr-status-monitor.py
```

Safe behavior:

- one Discord message per open PR;
- status-only channel content;
- if actionable issues appear, the monitor opens/reuses a PR-specific thread and pings the configured operator user once per active action;
- while an action is pending or a fix has been pushed, the monitor updates the status message but suppresses repeated thread pings;
- stale requested-changes reviews on an older SHA are marked `WAITING` / awaiting re-review rather than re-alerted as new work;
- stacked PRs whose base branch is another blocked open PR are marked `WAITING` on the parent instead of looking ready;
- merged PRs already tracked in the ledger have their status message marked merged and their PR-specific thread archived/locked so stale action threads close automatically;
- the monitor never pushes, merges, resolves GitHub comments, or changes GitHub PR state.

Actionable issues currently include:

- requested changes reviews on the current head SHA;
- stale requested-changes reviews on older SHAs, reported as `WAITING` / awaiting re-review;
- stacked child PRs whose base branch is another open blocked PR, reported as `WAITING` on the parent;
- rebase/merge-state problems (`dirty`, `behind`);
- failing/timed-out/cancelled/action-required checks;
- review or issue comments from non-operator users containing action words like `fix`, `need`, `rebase`, `conflict`, or `failing`.

## Rebase autocure

When a PR is blocked by rebase/conflicts, use:

```bash
python3 scripts/pr-rebase-autocure.py owner/repo#123
```

Default mode prepares a local worktree, checks out the PR, fetches the base branch, and attempts the rebase. It does **not** push.

Only after explicit operator authorization for that PR/branch update:

```bash
python3 scripts/pr-rebase-autocure.py owner/repo#123 --authorized-push
```

This uses `git push --force-with-lease` after a clean rebase. If conflicts occur, it exits nonzero and reports the worktree path so an interactive fix session can continue there.

## Recommended live wiring

- `submit_pr_ready` / handoff workflows call `scripts/pr-readiness-gate.py` directly. This is event-based, not cron-based.
- GitHub webhook handling should call `scripts/github-pr-status-monitor.py` or equivalent targeted sync when PR review/comment/check events arrive.
- A low-frequency cron sweeper may run the monitor as a reconciliation backup so missed webhook events do not hide stale review feedback.
