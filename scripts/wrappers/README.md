# Hermes wrapper scripts (install target: ~/.hermes/scripts/)

Source-of-truth copies of the bk-*.sh wrappers that the pipeline invokes from
`~/.hermes/scripts/`. Copy these into the install location after a fresh clone:

    cp scripts/wrappers/*.sh ~/.hermes/scripts/ && chmod +x ~/.hermes/scripts/bk-*.sh

- `bk-ci-parity-gate.sh` — deterministic CI-parity gate (tsc baseline + guards + bundle
  parity + run-ci-tests.sh) run by readiness-runner.py before PR publish.
- `bk-hermes-builder.sh` — builder wrapper; includes the `export PATH="$HOME/.local/bin:$PATH"`
  fix so `hermes` resolves in the cron/non-login environment (else exit 127).
