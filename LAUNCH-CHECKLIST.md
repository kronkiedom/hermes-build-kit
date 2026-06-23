# Launch checklist for a fresh Hermes environment

Use this when you are about to hand the bootstrap process to another Hermes environment.

## Preconditions
- [ ] The fresh Hermes environment can read the bootstrap repo.
- [ ] The fresh Hermes environment can read and modify the target repo.
- [ ] You know the absolute path to the bootstrap repo in that environment.
- [ ] You know the absolute path to the target repo in that environment.
- [ ] Hermes has the file + terminal tools available.
- [ ] If you expect cron setup later, Hermes has cron capabilities available in that environment.

## Variables to fill in
- `BOOTSTRAP_REPO_PATH=<absolute path to hermes-build-kit>`
- `TARGET_REPO_PATH=<absolute path to target repo>`

## Session start recommendation
Start a fresh Hermes session so the handoff is clean and the response only covers the current prompt.

## Copy-paste sequence

### Message 1
Open `prompts/operator-message-01.md`, replace the two placeholder paths, and send it.

Wait for Hermes to finish and verify:
- created files
- chosen local roots
- unresolved decisions
- verification output

Do not continue if verification failed.

### Message 2
Open `prompts/operator-message-02.md`, replace the same two placeholder paths, and send it.

Wait for Hermes to finish and verify:
- created files/directories
- state model summary
- verification output
- unresolved decisions

Do not continue if verification failed.

### Message 3
Open `prompts/operator-message-03.md`, replace the same two placeholder paths, and send it.

Wait for Hermes to finish and verify:
- helper coverage
- exact commands run
- generated status/dashboard outputs
- runtime assumptions

Do not continue if verification failed.

### Message 4
Open `prompts/operator-message-04.md`, replace the same two placeholder paths, and send it.

Wait for Hermes to finish and verify:
- created jobs
- worker mapping
- verification output
- blockers or policy deviations

Do not continue if verification failed.

### Message 5
Open `prompts/operator-message-05.md`, replace the same two placeholder paths, and send it.

Wait for Hermes to finish and verify:
- checks run
- pass/fail by subsystem
- evidence inspected
- blockers
- smallest next fix if needed

## Safe operating rule
Advance only one step at a time. If Hermes reports a blocker, missing capability, or failed verification, resolve that before moving to the next operator message.

## Suggested operator notes to capture
- chosen task root
- chosen backlog root
- chosen automation root
- approved policy deviations
- cron job names/IDs
- unresolved blockers
