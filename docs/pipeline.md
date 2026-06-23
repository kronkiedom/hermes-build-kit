# Portable Hermes build pipeline

This document defines a sanitized, project-agnostic build orchestration model for Hermes-driven work. It is intended to be adapted into a target repo, not run in place without repo-specific customization.

## Core principle
The conductor decides **what** should happen next. Durable files, explicit gates, and verification steps determine **whether** progression is allowed. Progress is recorded in artifacts, not implied by chat state.

## Lifecycle

```text
SHAPE -> CONTRACT-CHECKPOINT -> DRAFT-LOOP -> EXECUTE -> VERIFY-LOOP -> EVIDENCE -> RETRO -> DONE
```

Additional non-terminal states:
- `quota-paused`
- `escalated`
- `parked`

## Phase meanings

### SHAPE
Purpose:
- turn an operator request or admitted backlog item into a concrete contract
- define scope, acceptance criteria, exclusions, dependencies, and deliverables

Primary artifact:
- `task.md`

Outputs should answer:
- what problem is being solved
- what exact result counts as done
- what is out of scope
- what repo surfaces may be touched
- what proof will be required later

### CONTRACT-CHECKPOINT
Purpose:
- obtain explicit operator approval of the written contract before planning/execution proceeds

Allowed outcomes:
- `APPROVE`
- `AMEND` (contract is revised, then checkpoint fires again)
- `REJECT`

Rule:
- downstream roles enforce the written contract, not implied intent

### DRAFT-LOOP
Purpose:
- produce a concrete plan or implementation draft
- critique that draft with independent review
- iterate until approved or escalated

Primary artifacts:
- `draft-vN.md`
- `critique-rN-<reviewer>.md`
- `responses-rN.md`

Rules:
- critiques must be persisted as files
- a reviewer should not be the same model/context that produced the draft when independence matters
- repeated unchanged critique loops should escalate rather than spin indefinitely

### EXECUTE
Purpose:
- perform the approved work in the target repo
- produce a verifier-readable summary of actual changes and evidence

Primary artifact:
- `summary.md`

Rules:
- only the executor writes repo changes
- execution must be grounded in the approved contract and latest approved draft
- summary must cite changed surfaces and commands actually run

### VERIFY-LOOP
Purpose:
- verify the execution structurally and semantically
- ensure acceptance criteria are met
- ensure changes are real, wired, and evidenced

Primary artifacts:
- `verify-rN.md`
- optional additional `critique-rN-<reviewer>.md` for independent dry-pass review

Rules:
- same-context self-verification is not sufficient for terminal success
- a claimed fix is not terminal; an independent dry pass is terminal
- missing evidence, broken citations, or unverifiable claims block completion

### EVIDENCE
Purpose:
- assemble the final evidence package and landing manifest
- capture the exact changed files and proof that supports closure or handoff

Primary artifact:
- `evidence.md`

Should include:
- deliverables produced
- changed paths
- commands executed
- outputs or summaries of outputs
- unresolved caveats
- whether landing is autonomous or requires operator action

### RETRO
Purpose:
- record reusable lessons
- propose skill/process updates
- identify recurring friction

Outputs may include:
- skill patch proposals
- automation improvements
- memory candidates for operator approval

Rule:
- retro is not self-congratulation; it is structured improvement work

### DONE
Purpose:
- mark the task complete only after all gates have cleared and final evidence is durable

Rule:
- `DONE` is a file-backed state, not a conversational claim

## Durable artifact contract
Each real task should have a durable directory containing at least:

```text
<task-root>/<task-id>/
  meta.json
  task.md
  draft-vN.md
  critique-rN-<reviewer>.md
  responses-rN.md
  summary.md
  verify-rN.md
  evidence.md
  checkpoints.md
```

## Artifact rules
- Use absolute paths in role prompts and operational docs whenever possible.
- Write artifacts atomically: write temp file, then rename into place.
- Missing artifact means "not produced", not "implicitly complete".
- `meta.json` is the state source of truth.
- `checkpoints.md` is the human-readable audit trail of operator decisions.

## Minimum meta.json expectations
Each task's `meta.json` should record:
- `task_id`
- `created`
- `state`
- `phase_status`
- round counters
- `operator_approvals`
- `escalations`
- `last_completed_step`
- `awaiting_operator`
- `state_reason`
- optional `producer_model`
- optional `landing_manifest`

## Completion doctrine: dry-stop, not fix-stop
A fix is not enough.

A task reaches terminal green only when:
1. the fix exists,
2. the fix has been re-checked independently,
3. no new blocking issues are found,
4. the blast radius has been re-swept,
5. the evidence is persisted.

Implications:
- the producer does not get to declare its own work complete
- a clean self-check is useful but not terminal
- an independent dry pass is the terminal confirmation

## One-active-task rule
Unless a target repo explicitly chooses otherwise, the default automation posture is:
- only one active execution task at a time
- backlog discovery and soft-prep may continue in the background
- new admissions wait while an execution task is active

This reduces queue sprawl and makes dashboards and operator decisions easier to trust.

## Backlog / admission model
Backlog candidates are not executable work by themselves.

Recommended layers:
1. **Discovery candidate** — grounded possible work item, not yet approved
2. **Prepared candidate** — next-up packet or soft-prep package for operator inspection
3. **Admitted task** — becomes a real task directory and enters `SHAPE`

Rules:
- backlog presence is not approval
- admission should leave a durable link between candidate and task
- automation should read durable queue/index files, not prior chat text

## Escalation triggers
Escalate instead of silently continuing when any of these occur:
- contract ambiguity or contradiction
- repeated non-converging review loops
- missing required infrastructure or credentials
- scope expansion beyond the written contract
- quota exhaustion or dead provider pool
- malformed artifacts after repair attempts
- verification drift or evidence gaps
- irreversible or security-sensitive operations requiring operator judgment

## Stop conditions
A phase should stop and await operator or recovery action when:
- explicit operator approval is required
- a checkpoint requests amendment/rejection
- an escalation verdict occurs
- environmental failure blocks progress
- verification cannot prove the claims
- policy gates forbid autonomous landing

## Role boundaries
A portable implementation typically includes these logical roles:
- **conductor** — manages state machine and dispatch order
- **pm/shaper** — writes the contract
- **drafter/planner** — writes drafts/plans
- **executor** — performs repo changes
- **verifier** — checks structure and evidence
- **critic/reviewer** — independent review voice
- **retro analyst** — improvement pass

Hard rule:
- only the executor writes repo code or docs under normal flow

## Automation hooks reserved for later phases
Portable systems often add:
- backlog discovery worker
- soft-prep worker
- prep-admission worker
- dispatch worker
- discovery governor
- stall detector
- dashboard/status renderer

These should consume durable ledgers and status files rather than scrape chat logs.

## Adaptation requirement
This document defines invariants. A target repo still needs local decisions for:
- directory locations
- approval channel
- eligibility classes
- build/test commands
- landing policy
- automation schedules
- repo-specific risk boundaries
