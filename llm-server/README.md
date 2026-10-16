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

## Isolated container validation

The hash-locked worker passed a fresh container deployment and 17 real protocol
requests on 2026-10-16: 14 full replies and 3 stream closures followed by successful
recovery. The initial startup failure and unsuccessful build attempts are retained.
See the [container validation report](../docs/CONTAINER-LLM-VALIDATION.md) for raw
records, environment provenance, minimal build/run commands and limitations.

`deploy/Dockerfile.cached-wheels` is the validated installation path. It requires
all 63 original locked wheels and disables network during Python installation.
Base-image and APT retrieval still require network. `deploy/Dockerfile` provides
the online-install alternative; that path encountered download failures in this
run. Both use the same lock, isolated `/opt/worker` environment, non-root numeric
user, and writable cache identity (`USER=voxastage`, `HOME=/tmp`). Weights remain
outside the image and are mounted read-only.

`deploy/container_trial.py` supervises the actual GPU protocol. It checks daily
service occupancy, bounds health probes, preserves partial output and only cleans
up containers matching the full ID, image and run label. The separate
`deploy/check_guard_docker.py` exercises CPU Docker faults. The report gives their
validation boundaries; passing one worker does not validate an entire deployment.

The container shares the host kernel and GPU driver. Direct HTTP first-text timing
is not microphone-to-audio latency, and protocol success is not answer-quality
acceptance. ASR, TTS, avatar workers and independent-host validation remain separate.
