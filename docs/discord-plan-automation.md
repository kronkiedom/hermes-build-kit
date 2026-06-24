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

`scripts/discord-plan-poller.py` polls the build-control channel. For every new operator message containing `build plan:` it:

1. extracts the plan text;
2. creates one Discord thread for the plan;
3. writes durable artifacts under `plans/<plan-id>/`;
4. updates `.automation/plans-index.json`;
5. posts a short accepted/status message in build-control.

The first durable state is `CONTRACT`; execution does not begin until the plan is shaped into a contract.

## Contract shaping

Run:

```bash
python3 scripts/shape-plan-contract.py --plan-id <plan-id>
```

If the plan is ambiguous, the script writes `questions.md`, sets the plan state to `QUESTION`, and sets `awaiting_operator: true`.

For a controlled/manual approval during development:

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

This is the intake/contract/decomposition MVP. It does not yet implement the full PR executor loop. The next worker to build is `dispatch-pr-worker.py`, which should use isolated git worktrees, create draft PRs via `gh`, and write `summary.md`/`evidence.md` for verifier review.
