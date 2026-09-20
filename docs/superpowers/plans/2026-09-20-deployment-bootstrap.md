# Deployment Bootstrap Implementation Plan

> Use bounded implementation tasks and independent code review. Existing model processes and the production application must remain untouched.

**Goal:** One portable check/run entry point with honest dependency readiness and reproducible setup instructions.

**Architecture:** `deployment_check.py` holds literal environment loading and selected-chain probes. `demo.py` handles interpreter/resource checks, CLI presentation and foreground launch. Reuse current provider/dialogue/profile registries; defer renderer imports until building actual drivers.

## 1. Selected-chain diagnostics
- [x] Add `test_deployment_check.py` first: literal `.env` parsing, precedence, malformed runtime config, independent backend selection, correct profile speaker, API unverified states, MiniCPM vision capability, DINet reachability limitation, missing idle, bounded non-JSON/error/redirect responses and no credential disclosure.
- [x] Implement `build_environment(root, env_file=None, environ=None) -> dict` with 64KiB file bounds and no evaluation. Absent default `.env` is allowed; explicitly requested missing file is an error.
- [x] Implement `inspect_deployment(root, env, *, backend=None, profile=None, require_vision=False, timeout=3, request_json=None, tcp_check=None) -> dict` containing ready/backend/profile/provider/checks. Each check has name/status (`pass`, `warn`, `fail`), required, message and help; no raw endpoint or exception data.
- [x] Move renderer imports into `Provider.make_backend` and allow `PIPECAT_AVATAR_PROFILE` as the default-profile environment override so CLI selection reaches the actual UI without mutation. Keep browser override rules unchanged.
- [x] Run focused diagnostics and provider/profile regressions.

## 2. CLI and portable start
- [x] Add `test_demo.py` proving failed checks never start, selected configuration reaches child unchanged, relative resources resolve from project root, .venv/current or explicit interpreter checks are bounded, port collision preserves owner, and machine-readable output contains no secrets.
- [x] Implement `demo.py check|run [--env-file PATH] [--backend cascade|minicpm] [--profile ID] [--require-vision] [--python PATH] [--port 18314] [--json]` using existing bot.py entry point and localhost only.
- [x] Verify Python3.11, locked application dependency versions and NLTK resources with selected interpreter; no GPU packages are imported by diagnostics. Check may report failures before installation and show the install command.
- [x] Foreground run uses argument arrays and the verified environment. It does not stop or replace processes, install models, write configuration, or own unrelated workers.

## 3. Documentation and validation
- [x] Update `.env.example`, DEPLOYMENT/README/STATUS and export whitelist. Provide minimal selected-backend recipes and links to each model worker's actual install/protocol requirements, distinguishing configuration/health/model inference.
- [x] Verify fresh exported directory check/run with prepared workers on a free temporary port, application routes and capability parity, then stop only that foreground process.
- [x] Independent requirements/code review, focused plus required regression tests, clean export and a reviewable follow-up PR body. Keep PR #2 merged; do not reopen it or alter repository visibility.
- [x] Document blank GPU provisioning and streaming ASR as remaining stages rather than claiming them complete.
