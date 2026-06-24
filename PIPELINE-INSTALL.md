# Pipeline install — local adaptation decisions

This file records the concrete, repo-specific decisions for the live pipeline instance
installed into **hermes-build-kit** (self-install target). The generic contract lives in
`docs/pipeline.md`; this file is the adaptation layer required by `docs/adaptation-guide.md`.

Installed: 2026-06-24T01:26:41.664157+00:00

## Local roots (chosen)
| Concern | Location |
|---|---|
| Task root | `tasks/` |
| Backlog root | `.backlog/` |
| Soft-prep root | `.auto-prep/` |
| Automation root | `.automation/` |
| Status/dashboard outputs | `.automation/status/` |

## Approval representation
- Per-task: `operator_approvals[]` in `meta.json` + human-readable `checkpoints.md`
- Prep admission: explicit `.auto-prep/ack.json` consumed by `scripts/prep-admission.py`

## Eligibility classes eligible for automation
- `docs-only` (conservative default for this docs/templates/scripts repo)

## Verification commands (run from repo root)
- `python3 scripts/collect-status.py`
- `python3 scripts/render-dashboard.py`
- `python3 scripts/auto-dispatch.py`
- `python3 scripts/discovery-governor.py`
- `python3 scripts/prep-admission.py`  (admits a task only if `.auto-prep/ack.json` exists)

## Landing policy
- No autonomous landing. All landing is operator-gated.
- Autonomy `enabled: false` during bootstrap (see `.automation/AUTONOMY.json`).

## Automation schedule
- Deferred to Prompt 4. No live cron jobs created yet (kit safe-default: verify before cron).

## Invariants preserved (from docs/pipeline.md)
durable file-backed artifacts · explicit checkpoints · dry-stop completion ·
one-active-task default · backlog != approval · executor-only writes

## Unresolved decisions (require operator input before cron enablement)
1. Approve Prompt 4 to create live cron workers (currently held).
2. Confirm eligibility classes beyond `docs-only`, if any.
3. Confirm Gate-2 push target = dom-armor account/repo (pending account switch).
