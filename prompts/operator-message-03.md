# Operator message 03 — create automation helpers

Use this as the exact message to send to the fresh Hermes environment after Prompt 2 has completed successfully and been verified.

---

Continue in the same target repo.

First, read the bootstrap repo at `<BOOTSTRAP_REPO_PATH>`:
- `docs/automation-architecture.md`
- `docs/adaptation-guide.md`
- `scripts/collect-status.py`
- `scripts/render-dashboard.py`
- `scripts/discovery-governor.py`
- `scripts/prep-admission.py`
- `scripts/auto-dispatch.py`
- `prompts/03-create-automation-state.md`

Then inspect the target repo's current task, backlog, and automation roots from Prompts 1 and 2.

Your task is to complete only **Prompt 3 / create automation state and helpers**.

Requirements:
- Install or adapt helper scripts for status collection, dashboard rendering, discovery governance, prep admission, and dispatch decisions.
- Keep all helper behavior grounded in durable files in the target repo.
- Add or adapt any missing state ledgers required by those helpers.
- Do **not** create live cron jobs yet.
- Do **not** claim the automation is active yet.
- Prefer standard-library, portable implementations unless the target repo already has an established runtime/tooling path.

Before making changes, explicitly report:
1. which helper scripts or equivalent mechanisms already exist in the target repo,
2. what helper files you will add or adapt now,
3. any runtime assumptions you need to make (for example Python path, shell, or repo-local script location).

Then make the changes.

Before finishing, verify with real tool output that:
- each helper script exists,
- each helper script is readable,
- each non-destructive helper script runs successfully in the target repo,
- durable status/dashboard outputs are created where expected.

Final response format:
1. `Created or updated files` — exact paths
2. `Helper coverage` — status collection / dashboard / governor / prep admission / dispatch
3. `Verification` — exact commands run and results
4. `Runtime assumptions` — concise bullets
5. `Unresolved decisions` — concise bullets

Do not continue to Prompt 4 unless I explicitly tell you to.
