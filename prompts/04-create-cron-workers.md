# Prompt 4 — create cron workers

Read this repository first, especially:
- `docs/automation-architecture.md`
- `docs/operator-runbook.md`
- the installed helper scripts in the target repo

Then inspect the target repo's adapted task/backlog/automation roots and Hermes capabilities.

Your job in the target repo/environment:
- create the durable Hermes cron jobs or equivalent scheduled workers needed for the automation loop
- wire them to the repo-local scripts and docs you already installed
- keep schedules conservative and auditable

Recommended worker set:
- backlog discovery worker
- soft-prep worker
- prep-admission worker
- auto-dispatch worker
- discovery governor
- dashboard/status refresh worker
- optional stall detector

Portable default policy:
- every 5 minutes
- one active execution task at a time
- pause discovery at high watermark 10
- resume at low watermark 3
- quiet or stop after 3 empty discovery passes unless target policy differs

Requirements:
- do not guess unsupported Hermes features; inspect the actual environment first
- keep jobs self-contained and durable
- prefer repo-local state files over chat/session memory
- report exact job names, schedules, and script or prompt targets created
