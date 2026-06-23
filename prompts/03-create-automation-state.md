# Prompt 3 — create automation state and helpers

Read this repository first, especially:
- `docs/automation-architecture.md`
- `docs/adaptation-guide.md`
- `templates/AUTONOMY.example.json`

Then inspect the target repo's current task, backlog, and ops layout.

Your job in the target repo:
- create the automation state root
- add durable ledgers and status artifact locations
- install sanitized helper scripts adapted to the target repo's chosen paths
- keep all automation read/write behavior grounded in durable files

At minimum, create/adapt:
- autonomy config
- dispatch ledger
- approvals ledger or approval-state file
- worker status outputs
- dashboard outputs
- helper scripts for status collection and dashboard rendering

Do not create live cron jobs yet.
Do not claim the automation is active yet.

Requirements:
- adapt paths and naming to the target repo
- preserve the one-active-task and backlog!=approval rules unless the repo explicitly chooses otherwise
- verify every created file exists
- report unresolved repo-specific policy decisions still needed before cron enablement
