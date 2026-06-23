# Operator runbook

This runbook explains how a human operator interacts with the portable Hermes build process after it is adapted into a target repo.

## Normal flow
1. Discovery creates backlog candidates.
2. Soft-prep surfaces the next candidate.
3. Operator approves or rejects admission.
4. A real task is created and enters `SHAPE`.
5. Operator approves the written contract at `CONTRACT-CHECKPOINT`.
6. Planning, execution, verification, and evidence proceed.
7. If a gate fails or policy forbids autonomous landing, the task enters `escalated`.

## What to inspect
At minimum, inspect:
- the task's `meta.json`
- `task.md`
- `checkpoints.md`
- latest draft/critique files when planning is in progress
- `summary.md` and `verify-rN.md` during verification
- `evidence.md` before closure or landing

## What approval means
Approval should always apply to a written artifact, not a vague intent.

Examples:
- approving a prepared backlog candidate for admission
- approving the contract at `CONTRACT-CHECKPOINT`
- approving a non-autonomous landing step

## What escalated means
`escalated` means the system intentionally stopped because it hit a condition that should not be auto-guessed through.

Typical causes:
- scope conflict
- failed verification
- queue-policy conflict
- credential/infrastructure failure
- ambiguous landing or deployment risk

## Healthy operator posture
- prefer approving written contracts, not vague summaries
- prefer changing policy in config/docs rather than in ephemeral chat
- inspect dashboard artifacts for queue health
- require evidence before accepting completion claims
