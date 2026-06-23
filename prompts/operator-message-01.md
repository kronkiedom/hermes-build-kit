# Operator message 01 — bootstrap a new target repo

Use this as the exact message to send to a fresh Hermes environment after it can access both:
- this bootstrap repo, and
- the target repo where the process should be installed.

---

You are installing a portable Hermes build pipeline into a fresh target project.

First, read the bootstrap repo at `<BOOTSTRAP_REPO_PATH>`:
- `README.md`
- `QUICKSTART.md`
- `docs/pipeline.md`
- `docs/adaptation-guide.md`
- `docs/operator-runbook.md`
- `prompts/01-bootstrap-process.md`

Then inspect the target repo at `<TARGET_REPO_PATH>`.

Your task is to complete only **Prompt 1 / bootstrap project-local process**.

Requirements:
- Create a project-local version of the portable pipeline in the target repo.
- Choose repo-appropriate roots for tasks, backlog, and automation state.
- Install the canonical process docs into the target repo in adapted form.
- Create durable template/state stubs needed for the local process foundation.
- Do **not** create live cron jobs yet.
- Do **not** assume the target repo should use the exact same paths or names as the bootstrap repo.
- Preserve the invariants from `docs/pipeline.md`.

Before making changes, explicitly report:
1. the target repo paths you propose for tasks, backlog, and automation,
2. any repo-specific decisions that must be made during adaptation.

Then make the changes.

Before finishing, verify with real tool output that:
- every created file exists,
- the installed docs are readable,
- the local scaffold is present in the target repo.

Final response format:
1. `Created files` — exact paths
2. `Chosen local roots` — exact paths
3. `Unresolved decisions` — concise bullets
4. `Verification` — exact checks run and results

Do not continue to Prompt 2 unless I explicitly tell you to.
