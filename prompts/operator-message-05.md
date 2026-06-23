# Operator message 05 — verify automation end to end

Use this as the exact message to send to the fresh Hermes environment after Prompt 4 has completed successfully and been verified.

---

Continue in the same target repo/environment.

First, read the bootstrap repo at `<BOOTSTRAP_REPO_PATH>`:
- `docs/pipeline.md`
- `docs/automation-architecture.md`
- `docs/operator-runbook.md`
- `prompts/05-verify-automation.md`

Then inspect the target repo's installed workflow, helper scripts, and cron/scheduler configuration.

Your task is to complete only **Prompt 5 / verify the automation end to end**.

Requirements:
- Verify the installed workflow using real files and real tool output.
- Do not merely restate intended behavior.
- Use the target repo's actual state roots, helper scripts, and job configuration.
- If safe and possible, run a non-destructive dry test using sample or mock candidate/task inputs.

At minimum verify:
1. pipeline docs and task/backlog/automation roots exist,
2. durable state artifacts exist and are readable,
3. helper scripts run successfully,
4. dashboard/status outputs render successfully,
5. autonomy config parses and matches the intended policy,
6. cron jobs or equivalent workers exist and point at expected targets,
7. one-active-task guard behaves as intended in the current state model.

Before making changes or test mutations, explicitly report:
1. what you plan to verify,
2. what safe dry-test inputs you will use,
3. any verification limits or blockers.

Then run the checks.

Final response format:
1. `Checks run` — exact commands/actions
2. `Pass/fail by subsystem` — docs, state roots, helper scripts, dashboard, autonomy config, scheduler, dispatch guard
3. `Evidence` — exact files/jobs inspected
4. `Blockers` — concise bullets
5. `Smallest next fix` — concise bullets, only if something failed

If any subsystem fails, do not silently repair and continue unless I explicitly ask for remediation.
