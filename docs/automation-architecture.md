# Portable automation architecture

This document describes the sanitized automation model that can sit on top of the portable Hermes build pipeline.

## Automation goals
- keep backlog discovery durable
- keep operator review lightweight and explicit
- avoid starting work from chat state alone
- enforce queue discipline
- expose clear health/status artifacts

## Recommended durable areas
A target repo should usually create three durable roots:
- task root
- backlog root
- automation root

Suggested contents:

### Task root
Stores admitted tasks and all task artifacts.

### Backlog root
Stores discovery candidates and queue index state.

### Automation root
Stores:
- autonomy config
- dashboard outputs
- worker status ledgers
- dispatch ledger
- approvals ledger
- pause/governor state

## Worker model

### 1. Backlog discovery worker
Discovers potential work and writes or updates backlog candidates.

Rules:
- must be repo-grounded
- must not auto-admit execution work
- should update a durable queue/index

### 2. Soft-prep worker
When execution is idle, prepares the next likely candidate for operator review.

Rules:
- reads queue state
- does not create real execution tasks
- outputs a durable prep artifact

### 3. Prep-admission worker
Consumes an explicit operator acknowledgement of a prepared candidate and converts it into a real task.

Rules:
- creates a real task directory
- records the source candidate link
- enters `SHAPE`
- does not bypass pipeline gates

### 4. Auto-dispatch worker
Moves a task forward only when the current machine state permits it.

Rules:
- one active execution task by default
- must honor awaiting-operator states
- should record dispatch history durably

### 5. Discovery governor
Pauses or resumes discovery based on queue pressure.

Portable default policy:
- pause discovery at a high watermark
- resume at a lower watermark
- stop or quiet after repeated empty passes

### 6. Dashboard/status renderer
Builds human-readable and machine-readable status from durable artifacts.

Outputs commonly include:
- `dashboard.md`
- `dashboard.json`
- last-worker-result ledgers

### 7. Stall detector
Flags tasks or workers that appear stuck.

## Suggested default policy
- one active task at a time
- idle checks every 5 minutes
- pause discovery when open queue reaches 10
- resume discovery when open queue drops to 3
- stop noisy discovery after 3 empty passes unless policy says otherwise

These are defaults, not invariants.

## Suggested ledgers
- `AUTONOMY.json`
- `dispatch-ledger.json`
- `approval-panel.json` or equivalent
- `status/backlog-discovery-last.json`
- `status/soft-prep-last.json`
- `status/prep-admission-last.json`
- `status/dashboard.json`
- `status/dashboard.md`
- optional governor state file

## Safety boundaries
Automation should stop and surface rather than guess when:
- approval is missing
- queue state is contradictory
- active task state is invalid
- required directories are absent
- repo-specific eligibility class is unclear
- landing policy forbids autonomous continuation
