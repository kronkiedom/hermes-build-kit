# Prompt 5 — verify the automation end to end

Read this repository first, especially:
- `docs/pipeline.md`
- `docs/automation-architecture.md`
- `docs/operator-runbook.md`

Then inspect the target repo's installed workflow, scripts, and cron configuration.

Your job:
- verify the local process works end to end using real files and real tool output
- do not merely restate intended behavior

At minimum verify:
1. pipeline docs and task directories exist
2. backlog candidate files are readable and distinguish candidate vs admitted task
3. automation status scripts run successfully
4. dashboard artifacts render successfully
5. autonomy config parses and matches the intended policy
6. cron jobs exist and point at the expected repo-local targets
7. one-active-task guard behaves as intended in the current state model

If safe and possible, run a dry test using:
- a sample backlog candidate
- a sample or mock prepared candidate
- a non-destructive status refresh or worker tick

Final response should include:
- exact checks run
- exact files/jobs inspected
- pass/fail by subsystem
- blockers preventing live enablement
- smallest next fix if something fails
