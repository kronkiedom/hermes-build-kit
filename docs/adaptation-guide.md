# Adaptation guide

This kit is portable only if the target environment adapts it deliberately.

## Required local decisions
A fresh Hermes environment must decide, for the target repo:
- where task directories live
- where backlog candidates live
- where automation ledgers live
- how approval is represented
- what task classes are eligible for automation
- what build/test/verification commands exist
- whether autonomous landing is allowed for docs, code, both, or neither
- what dashboard destination exists, if any

## Recommended adaptation order
1. inspect the target repo structure
2. choose project-local roots for tasks/backlog/automation
3. install the pipeline spec into the target repo
4. create durable templates and state stubs
5. wire helper scripts to the target repo's paths
6. only then create cron workers

## What should stay invariant
Try to preserve these across repos:
- durable artifacts
- explicit checkpoints
- dry-stop completion doctrine
- one-active-task default
- backlog != approval
- executor-only write rule

## What may vary
These are expected to vary:
- path names
- naming conventions
- model choices
- verification commands
- approval surface
- landing policy
- queue thresholds
