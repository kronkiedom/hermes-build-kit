timestamp: 2026-06-24T01:26:41.664157+00:00
candidate_id: candidate-stall-detector
title: Add a stall-detector worker to the automation set
category: automation
tags:
  - automation
  - reliability
status: implemented
eligibility_class: docs-only
source_paths:
  - /home/armoruser/hermes-build-kit/docs/automation-architecture.md
note: discovery candidate (not approved); demonstrates backlog != approval

## Why it belongs in the queue
docs/automation-architecture.md lists a stall detector (worker #7) as a recommended
worker, but no starter implementation exists in scripts/. This is grounded, optional work.

## Evidence
- docs/automation-architecture.md § "7. Stall detector"
- scripts/ contains no stall-detector starter

## Queue intent
- approved by operator on 2026-06-25 and implemented as `scripts/stall-detector.py`
- status output: `.automation/status/stall-detector-last.json`
