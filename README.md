# hermes-build-kit

Sanitized, portable starter kit for building a checkpoint-gated Hermes delivery process in a new project.

## Purpose
This repository is a bootstrap source for a fresh Hermes environment that needs to stand up:
- a durable task pipeline
- auditable role artifacts
- backlog and admission lanes
- future automation hooks
- optional cron-driven workers

It is intentionally **project-generic**. It defines invariants and templates, then expects the target environment to adapt them to the repo it is actually working in.

## What this kit contains
- `docs/pipeline.md` — canonical lifecycle, gates, invariants, and artifact contract
- `docs/automation-architecture.md` — portable automation model and worker boundaries
- `docs/operator-runbook.md` — how a human operator interacts with the system
- `docs/adaptation-guide.md` — what must be customized per target repo
- `prompts/` — stepwise prompts for a fresh Hermes environment
- `templates/` — starter JSON/Markdown artifacts
- `scripts/` — optional starter utilities for status collection and dashboards
- `examples/` — sample task/backlog layouts

## Intended usage
1. A fresh Hermes environment clones or reads this repo.
2. It inspects the target project repo.
3. It adapts this framework into that target repo.
4. It implements the process one prompt at a time.
5. It verifies the resulting local workflow before enabling automation.

## Non-goals
- This kit is not tied to one codebase.
- It does not assume one model provider or one messaging platform.
- It does not auto-create live cron jobs by itself.
- It does not replace repo-specific build/test commands.

## Initial bootstrap sequence
- Prompt 1: create project-local pipeline scaffold
- Prompt 2: create durable task/backlog/autonomy state
- Prompt 3: add automation helpers
- Prompt 4: create cron workers
- Prompt 5: verify end-to-end behavior

## Operator-message workflow
If you are driving a separate Hermes environment by hand, use the operator-facing prompt files in `prompts/`.

Recommended order:
- `prompts/operator-message-01.md`
- `prompts/operator-message-02.md`
- `prompts/operator-message-03.md`
- `prompts/operator-message-04.md`
- `prompts/operator-message-05.md`

How to use them:
1. Replace `<BOOTSTRAP_REPO_PATH>` with the path to this repo in the fresh environment.
2. Replace `<TARGET_REPO_PATH>` with the path to the repo where the pipeline should be installed.
3. Send only one operator-message file at a time.
4. Wait for verification before sending the next one.
5. Stop after any failed verification and remediate before advancing.
