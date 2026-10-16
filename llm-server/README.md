# Local streaming LLM environment

The root `llm_server.py` serves local Qwen text generation as incremental NDJSON. It uses upstream Transformers and BitsAndBytes NF4 quantization; this project supplies the HTTP streaming worker and application/tool integration, not the model or quantization algorithm.

`deploy/requirements-standalone.in` fixes the observed core versions. The complete SHA256 lock targets Linux x86_64 and Python3.12. Install independently of the application and other model environments:

```sh
uv venv --python /path/to/python3.12 runtime/llm-standalone
uv pip sync --python runtime/llm-standalone/bin/python --require-hashes \
  llm-server/deploy/requirements-standalone.lock
uv pip check --python runtime/llm-standalone/bin/python
```

Do not create this environment with `--system-site-packages`. Clear external `PYTHONPATH`, `PYTHONHOME` and `LD_LIBRARY_PATH` before starting, and disable user packages. Supply an authorized local model directory; no weights are downloaded by the worker.

```sh
env -u PYTHONPATH -u PYTHONHOME -u LD_LIBRARY_PATH PYTHONNOUSERSITE=1 \
  runtime/llm-standalone/bin/python -I llm_server.py \
  --model /path/to/Qwen3-4B --port 18311 --quantization 4bit
```

The service listens only on localhost. `GET /health` reports readiness and the inference lock; `POST /generate` accepts `messages` and `max_new_tokens` (1–256), and returns newline-separated JSON text deltas. Streaming errors use `error`; HTTP409 means generation still owns the single-worker lock. A client disconnection requests cooperative generation stop; it does not forcibly terminate a GPU kernel. The model holds admission until the generation thread exits.

The application uses `PIPECAT_LLM_URL=http://127.0.0.1:18311`. A ready health response alone does not prove usable replies, tool correctness or answer quality. This installation lock does not include a GPU driver or system libraries, and does not validate a fresh operating system. See the [validation report](../docs/LLM-STANDALONE-VALIDATION.md): 63 isolated packages, 21 protocol-stage generations, persistent service restoration and a separate three-round real retrieval regression. Original failed and incorrect-answer cases remain disclosed.

## Container validation in progress

`deploy/Dockerfile` starts from a digest-pinned Python3.12 Debian image, installs
`libgomp1`, and installs the same hash-locked packages into `/opt/worker`. Runtime
uses a non-root user and isolated Python mode. The initial build timed out while downloading a dependency. A retry with a120s
read timeout received only part of a large wheel and correctly failed its hash
check. Both failures are retained.
This image has not yet passed the frozen17-request protocol check. See the
[deployment plan](../docs/plans/2026-10-16-container-deployment.md) for scope and
remaining checks.

Before running any GPU trial, the host controller must recheck daily-service
health after model hashing, continuously monitor occupancy, and stop only its
own container on a failed check. `deploy/container_guard.py` provides those
monitoring and ownership primitives; it is not the complete trial controller.
Every mutation verifies full container ID, immutable image ID and a nonempty
run label. A failed probe remains failed even if the service later becomes idle.
The200ms interval is between completed bounded probes, not a maximum reaction
time.

Run the CPU unit checks without Docker:

```sh
python llm-server/deploy/test_container_guard.py
```

An optional Docker fault drill creates network-isolated CPU-only containers and
checks occupancy/timeout aborts, rejection of a different run label, retained
partial logs, and cleanup. Supply the immutable ID of an already-present Python
image and a new output directory:

```sh
python llm-server/deploy/check_guard_docker.py \
  --image "$PYTHON_IMAGE_ID" --output /tmp/voxastage-guard-drill-new
```

This drill does not load a model or validate GPU inference. Container validation
also shares the host kernel and GPU driver, and cannot replace full installation
on an independent blank GPU host.

For unreliable large downloads, `deploy/Dockerfile.cached-wheels` accepts a
`wheels/` directory in its minimal build context. It mounts original wheel files
read-only during installation and uses `--find-links`; all packages still have
to match the unchanged lock hashes. This does not copy an installed host
environment. Wheels not in that directory are downloaded normally. Keep wheel
files out of git. Record their filenames, sizes and SHA256 before transfer and
verify them again on the build host. BuildKit is required for this variant.

The CPU Docker fault drill passed both injected cases on2026-10-16. Both
containers rejected a different run label, retained their logs, stopped once
and were removed. Docker escalated after the configured5s stop timeout
(exit137), so this is evidence of bounded termination, not graceful shutdown.
It does not cover controller TERM/HUP, Docker creation timeouts or GPU work.
