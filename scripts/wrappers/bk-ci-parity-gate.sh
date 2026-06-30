#!/usr/bin/env bash
# CI-parity pre-PR gate. Mirrors .github/workflows/validate.yml job
# "Code checks (tsc / tests / bundle)". Run from INSIDE the task worktree (cwd).
# Exits nonzero if ANY check fails so the readiness runner fails closed BEFORE
# publishing a PR. Deterministic complement to the LLM readiness auditor, which
# only runs targeted tests for changed surfaces and misses cross-cutting/full-suite failures.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

fails=()
run_check() {
  local name="$1"; shift
  echo "---- ${name} ----"
  if "$@"; then
    echo "  PASS: ${name}"
  else
    local rc=$?
    echo "  FAIL: ${name} (exit ${rc})"
    fails+=("${name}")
  fi
}

# CI runs `npm ci`; worktrees usually already have node_modules.
if [[ ! -d node_modules ]]; then
  echo "node_modules missing -- running npm ci"
  npm ci --no-audit --no-fund || { echo "  FAIL: npm ci"; exit 1; }
fi

[[ -f .github/scripts/check-tsc-baseline.sh ]]        && run_check "tsc-baseline-guard"        bash .github/scripts/check-tsc-baseline.sh
[[ -f .github/scripts/check-chat-per-user-boundary.cjs ]] && run_check "chat-per-user-boundary" node .github/scripts/check-chat-per-user-boundary.cjs
[[ -f .github/scripts/check-enforced-query-args.mjs ]] && run_check "enforcedQuery-guard"      node .github/scripts/check-enforced-query-args.mjs
[[ -f .github/scripts/check-single-gate-engine.mjs ]]  && run_check "single-gate-engine-guard" node .github/scripts/check-single-gate-engine.mjs
[[ -f .github/scripts/check-migrations-registry.mjs ]] && run_check "migrations-registry-guard" node .github/scripts/check-migrations-registry.mjs
[[ -f agent-runtime/swarm-executor.js ]]               && run_check "esbuild-parity-swarm-executor" npx esbuild agent-runtime/swarm-executor.js --bundle --platform=node --format=esm --outfile=/dev/null
[[ -f api/rag/search-tool.ts ]]                        && run_check "esbuild-parity-rag-search"     npx esbuild api/rag/search-tool.ts --bundle --platform=node --format=esm --outfile=/dev/null
[[ -f tests/strategies/strategy-failclosed.test.js ]]  && run_check "strategy-fail-closed"     npx vitest run tests/strategies/strategy-failclosed.test.js
[[ -f agent-runtime/gate-logic.test.js ]]              && run_check "gate-logic"               npx vitest run agent-runtime/gate-logic.test.js
[[ -f .github/scripts/run-ci-tests.sh ]]               && run_check "curated-high-risk-suites" bash .github/scripts/run-ci-tests.sh

echo
if (( ${#fails[@]} > 0 )); then
  echo "CI-PARITY GATE: FAIL (${#fails[@]} failing: ${fails[*]})"
  exit 1
fi
echo "CI-PARITY GATE: PASS"
exit 0
