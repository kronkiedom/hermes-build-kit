# Operator message 04 — create cron workers

Use this as the exact message to send to the fresh Hermes environment after Prompt 3 has completed successfully and been verified.

---

Continue in the same target repo/environment.

First, read the bootstrap repo at `<BOOTSTRAP_REPO_PATH>`:
- `docs/automation-architecture.md`
- `docs/operator-runbook.md`
- `prompts/04-create-cron-workers.md`

Then inspect the target repo's installed workflow, helper scripts, and available Hermes cron capabilities.

Your task is to complete only **Prompt 4 / create cron workers**.

Requirements:
- Inspect the actual Hermes environment before assuming cron features or command names.
- Create durable, self-contained scheduled workers or jobs for the installed local workflow.
- Wire jobs to the repo-local scripts and durable state files already created.
- Keep schedules conservative and auditable.
- Preserve the default safety posture unless the target repo has an explicit policy override:
  - 5 minute cadence
  - one active execution task at a time
  - pause discovery at high watermark 10
  - resume discovery at low watermark 3
  - quiet or stop after 3 empty discovery passes unless local policy differs

Before making changes, explicitly report:
1. which scheduling mechanism exists in this Hermes environment,
2. which workers you intend to create,
3. what schedule each worker will use,
4. any blockers to live job creation.

Then make the changes.

Before finishing, verify with real tool output that:
- each created job exists,
- each job points at the expected local script or prompt target,
- the configured schedules match the reported policy,
- no job depends on chat-only context.

Final response format:
1. `Created jobs` — exact names/IDs/schedules
2. `Worker mapping` — worker -> script/prompt/target path
3. `Verification` — exact checks run and results
4. `Policy deviations` — concise bullets, if any
5. `Blockers` — concise bullets, if any

Do not continue to Prompt 5 unless I explicitly tell you to.
