#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
: "${BUILDER_PROMPT_PATH:?BUILDER_PROMPT_PATH is required}"
: "${BUILD_TASK_DIR:?BUILD_TASK_DIR is required}"

policy_file="${BK_MODEL_POLICY_FILE:-$HOME/.hermes/scripts/bk-model-policy.sh}"
# shellcheck source=/home/armoruser/.hermes/scripts/bk-model-policy.sh
source "$policy_file"
bk_resolve_model_policy builder
bk_write_model_metadata "$BUILD_TASK_DIR/builder-run-metadata.json" > /dev/null

invocation="$BUILD_TASK_DIR/builder-invocation.md"
cat > "$invocation" <<EOF
You are the build-control builder for an Armor Swarm PR packet.

Model policy enforcement:
- Workflow role: builder
- Required model tier: coding_working
- Actual provider/model selected by the wrapper: ${BK_MODEL_PROVIDER} / ${BK_MODEL}
- This builder output is INVALID-WITHOUT the wrapper-written metadata at BUILD_TASK_DIR/builder-run-metadata.json proving the coding_working tier was used.

Read the dispatch packet at BUILDER_PROMPT_PATH, then implement the requested code changes in the current working directory.

Hard rules:
- You are already in the isolated Armor Swarm worktree. Work only in this cwd.
- Verify GitHub.com main/source state before relying on branch freshness.
- Do not push, open a PR, merge, or alter production.
- Do not commit; leave file changes in the worktree for the outer build worker to commit after it captures evidence.
- Do not create placeholder/facade work. If you cannot implement the real slice safely, write a clear blocker file and exit nonzero.
- Keep scope to this packet.
- Run targeted tests or checks for touched surfaces; if a full baseline is noisy, record exact command output and changed-file diagnostics.
- Write any useful notes into the task directory pointed to by BUILD_TASK_DIR.

Dispatch packet follows:
EOF
cat "$BUILDER_PROMPT_PATH" >> "$invocation"

if [[ "${BK_MODEL_POLICY_DRY_RUN:-}" == "1" ]]; then
  printf 'DRY_RUN builder would launch Hermes with provider=%s model=%s\n' "$BK_MODEL_PROVIDER" "$BK_MODEL"
  exit 0
fi

hermes chat -Q --yolo --source tool --toolsets terminal,file,search,skills --max-turns 90 \
  --provider "$BK_MODEL_PROVIDER" --model "$BK_MODEL" \
  -s github-pr-workflow,systematic-debugging,test-driven-development \
  -q "$(cat "$invocation")"
