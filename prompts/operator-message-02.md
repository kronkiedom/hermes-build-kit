# Operator message 02 — create durable task/backlog/autonomy state

Use this as the exact message to send to the fresh Hermes environment after Prompt 1 has completed successfully and been verified.

---

Continue in the same target repo.

First, read the bootstrap repo at `<BOOTSTRAP_REPO_PATH>`:
- `docs/pipeline.md`
- `docs/automation-architecture.md`
- `templates/AUTONOMY.example.json`
- `templates/task-meta.example.json`
- `templates/backlog-candidate.example.md`
- `templates/task-checkpoints.example.md`
- `prompts/02-create-task-system.md`

Then inspect the target repo changes created during Prompt 1.

Your task is to complete only **Prompt 2 / create durable task/backlog/autonomy state**.

Requirements:
- Create the durable task directory contract in the target repo.
- Create the backlog directory contract in the target repo.
- Create the automation state root in the target repo.
- Add initial autonomy/config/ledger stubs.
- Create one sample task/bootstrap artifact set that demonstrates the local state model.
- Do **not** create live cron jobs yet.
- Preserve the invariants from `docs/pipeline.md` and `docs/automation-architecture.md`.

Before making changes, explicitly report:
1. which directories and durable files from Prompt 1 already exist,
2. what additional state/ledger files you will add now.

Then make the changes.

Before finishing, verify with real tool output that:
- every created directory exists,
- every created JSON/Markdown artifact exists,
- the autonomy/config files parse or read successfully,
- the sample task/bootstrap artifact set is structurally complete.

Final response format:
1. `Created files` — exact paths
2. `Created directories` — exact paths
3. `State model` — concise summary of task/backlog/automation roots and key ledgers
4. `Verification` — exact checks run and results
5. `Unresolved decisions` — concise bullets

Do not continue to Prompt 3 unless I explicitly tell you to.
