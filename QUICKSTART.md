# QUICKSTART

This repository is meant to be read by a fresh Hermes environment and then adapted into a different target repository.

## Fast path
1. Clone this repo.
2. Point Hermes at this repo so it can read `README.md`, `docs/`, `prompts/`, and `templates/`.
3. Give Hermes `prompts/01-bootstrap-process.md` first.
4. After it completes and verifies the local scaffold, give Hermes prompts `02` through `05` in order.
5. Only enable live automation after Prompt 5 proves the installed workflow works with real files and real commands.

## Recommended operator workflow
- Start in a clean Hermes session.
- Make sure Hermes can inspect both this bootstrap repo and the target repo.
- Require Hermes to report exact created files after each prompt.
- Require Hermes to verify scripts/jobs with real output before claiming success.

## Prompt order
- `prompts/01-bootstrap-process.md`
- `prompts/02-create-task-system.md`
- `prompts/03-create-automation-state.md`
- `prompts/04-create-cron-workers.md`
- `prompts/05-verify-automation.md`

## Adaptation rule
This kit defines invariants, not one mandatory folder layout. The target environment should preserve the process contract while adapting path names and repo-specific verification commands.

## Safe default
If a target repo is not ready for cron, stop after Prompt 3 and use the installed process manually until the local state model is trustworthy.
