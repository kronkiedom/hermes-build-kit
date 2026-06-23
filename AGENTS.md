# AGENTS.md

This repository is a portable bootstrap kit for creating a Hermes-driven build pipeline in another project.

## Purpose
When working in this repo, prioritize:
1. preserving portability,
2. keeping docs sanitized and project-generic,
3. requiring durable file-backed artifacts,
4. avoiding references to private/local-only paths from the originating environment.

## Rules
- Treat `docs/pipeline.md` as the canonical process contract for this repo.
- Treat `docs/adaptation-guide.md` as the canonical statement of what must be customized in target repos.
- Do not hard-code target-project paths in shared docs or templates.
- Do not claim automation works unless the scripts or jobs were actually run and their outputs inspected.
- Prefer durable ledgers and status files over chat-only state.

## Prompt authoring rules
- Prompts should tell a fresh Hermes to inspect the target repo before adapting this kit.
- Prompts should require reporting exact files created, exact jobs created, and unresolved decisions.
- Prompts should avoid project-specific naming unless explicitly templated.

## Script rules
- Scripts in `scripts/` are starters, not source-of-truth implementations.
- Keep them simple, portable, and easy to adapt.
- Favor standard library Python when possible.

## Verification rule
A change to this repo is not complete until the relevant script, prompt, or document has been inspected and, when applicable, executed with real tool output.
