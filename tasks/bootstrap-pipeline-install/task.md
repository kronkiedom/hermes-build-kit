# Task: bootstrap pipeline self-install

## Problem
Install a concrete, durable instance of the portable Hermes build pipeline into the
hermes-build-kit repo itself, so the kit ships with a live, verifiable reference instance.

## Done means
- repo-local roots exist: `tasks/`, `.backlog/`, `.auto-prep/`, `.automation/` (+ `status/`)
- autonomy config present and parseable, disabled in bootstrap phase
- helper scripts run from repo root and emit durable status/dashboard artifacts
- one-active-task guard observably behaves
- end-to-end verification (Prompt 5) passes before any cron is enabled

## Out of scope
- enabling live automation / cron (gated, operator approval required)
- autonomous landing of any changes
- pushing to dom-armor (deferred Gate 2)

## Surfaces touched
- repo-root durable roots and `.automation/` state only; generic `docs/`, `templates/`, `scripts/` left intact

## Proof required
- real script output for collect-status, render-dashboard, auto-dispatch, discovery-governor
- rendered `.automation/status/dashboard.md` + `.json`
