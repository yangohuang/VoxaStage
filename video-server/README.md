# FlashHead Lite streaming worker

This is a bridge around [SoulX-FlashHead](https://github.com/Soul-AILab/SoulX-FlashHead), implemented against source commit `9bc03de06bb0de82cd6bc477804512ae06144bf2`. Real RTX4090 inference, three complete clips, cancellation and recovery passed on 2026-09-18; see [acceptance record](../AVATAR-MULTIDRIVER-RESULTS.md). It uses the upstream Lite pipeline, `get_base_data` once for the fixed portrait, eight seconds of 16 kHz audio history, and the upstream 0.96-second (24-frame) streaming chunks.

The optional `single-gpu.patch` makes the upstream unconditional xfuser imports optional for a single GPU. Multi-GPU usage still fails explicitly without the real xfuser package. Apply to that exact checkout with `git apply /absolute/path/to/video-server/single-gpu.patch`. Source remains Apache-2.0; keep upstream licensing with any redistributed copy. This demo exports the patch, not the third-party checkout or weights.

Download only Lite, LTX VAE and wav2vec2 (about 8GB) with resumable ranges and SHA256 verification:

```bash
python video-server/fetch_models.py --output models --workers 8
```

Each model directory records the resolved revision in `source.json`; reruns reuse it. Completed LFS weights are checked against the official SHA256. Do not delete progress files while their downloads run. Weights and portraits are excluded from the source export.

Validated inference environment: Python3.10, torch2.7.1+cu128, torchvision0.22.1+cu128, transformers4.57.3, diffusers0.34.0, soxr1.1.0, pyloudnorm0.2.0. The torch/torchvision pairing follows the [official PyTorch 2.7.1 installation matrix](https://pytorch.org/get-started/previous-versions/#v271).

### Independent installation (Linux x86_64 / Python 3.10)

`requirements.in` lists the worker's inference dependencies; `requirements.lock` pins the full resolved dependency set with SHA256 hashes. It omits the unused, incompatible torchaudio inherited by the original local environment. The diagnostic `requirements.local.lock` is not an installation input and is excluded from the source release. `requirements.txt` contains only bridge dependencies for contract tests, not the model runtime.

Install Python 3.10 and `uv`, then run from the demo root:

```bash
bash video-server/install.sh
# Optional: FLASHHEAD_PYTHON=/path/to/python3.10 FLASHHEAD_ENV=/path/to/new/venv
```

This creates `runtime/flashhead-standalone` without system site packages, verifies wheel hashes, runs `uv pip check`, and checks core imports. It does not stop existing services or load GPU models. CUDA wheels are downloaded from the official PyTorch index; an NVIDIA driver compatible with CUDA 12.8 and glibc 2.28+ are required.

On 2026-09-18, a new independent environment installed all 103 locked packages successfully; dependency checking, upstream imports, eight contract tests, three actual GPU clips, cancellation/recovery, and Pipecat text/synthetic-WAV integration passed. Re-running the installer also passed without replacing packages. The current local worker uses this environment. This validates a new Python environment on the existing GPU host, not provisioning a fresh operating system. The original inherited environment remains available locally for rollback.

Prepare a new source checkout at the pinned revision (skip cloning if this exact checkout is already prepared):

```bash
mkdir -p runtime
git clone https://github.com/Soul-AILab/SoulX-FlashHead.git runtime/SoulX-FlashHead
git -C runtime/SoulX-FlashHead checkout --detach 9bc03de06bb0de82cd6bc477804512ae06144bf2
git -C runtime/SoulX-FlashHead apply "$(pwd)/video-server/single-gpu.patch"
runtime/flashhead-standalone/bin/python video-server/fetch_models.py --output models --workers 8
```

Use `runtime/flashhead-standalone/bin/python` for the worker command below. Supply your own authorized portrait; no example portrait or weights are bundled.

Run it from the isolated FlashHead environment. The lock covers the single-GPU worker; the upstream Gradio, distributed xfuser and xformers dependencies are not needed for this path:

```bash
python video-server/flashhead_worker.py \
  --source runtime/SoulX-FlashHead \
  --checkpoint models/flashhead-lite \
  --wav2vec models/wav2vec2-base-960h \
  --portrait /absolute/path/to/portrait.png \
  --host 127.0.0.1 --port 8203
```

The default is Lite only and disables `torch.compile` for initial validation. It preloads the model before binding the server, so a client does not spend its start-message timeout on cold initialization; use `--lazy-load` only for tests or diagnostic startup. Add `--enable-torch-compile` after a verified run. The service is single-model/single-GPU: one inference lease covers each clip, including cleanup after cancellation.

`GET /health` returns 503 until the model has loaded and 200 with `ready:true` afterwards. With default preload, it is ready when the server starts accepting requests. Health also reports `busy`, PyTorch GPU allocated/reserved/peak memory, and the last chunk's render/JPEG duration. `WS /v1/stream` uses protocol 1:

1. Client sends `{"type":"start","protocol":1,"sample_rate":24000}`.
2. Worker replies `metadata` with `kind:"2d"`, JPEG codec, 24 kHz, 25 fps, and 512×512 dimensions.
3. Client sends PCM16LE mono 24 kHz as binary messages (at most 32,000 bytes each), then `{"type":"end"}`; `cancel` interrupts a clip.
4. Worker emits `frame` JSON messages with zero-based `index`, `pts_seconds`, and base64 JPEG `image`, followed by `{"type":"done","cleanup_complete":true}`.

There are exactly `ceil(input_pcm_samples / 960)` frame messages. The final inference chunk may be zero-padded for the model, but padded frames are discarded and audio is never re-emitted or modified.

The contract tests use a fake JPEG engine. They verify protocol, tail accounting, cancellation cleanup ordering, message and duration limits; they are not a model inference validation:

```bash
python -m pytest -q video-server/tests
```

For real validation, use a downloaded Lite checkpoint and local wav2vec files, connect a PCM client, and record first-frame latency, GPU memory, input PCM samples and emitted frame count. The upstream project and weights have their own Apache-2.0 and model terms; obtain and use the portrait only with permission.

From the demo root, exercise the **actual** bridge with a local mono PCM16 WAV:

```bash
.venv/bin/python smoke_video.py --input /absolute/path/to/test.wav \
  --url ws://127.0.0.1:8203/v1/stream --rounds 3 --interrupt \
  --output artifacts/flashhead-model
```

The script checks original PCM equality, frame/sample continuity, three completed clips and cancellation followed by recovery. It saves first/last JPEG frames plus timings for visual review. `--realtime` feeds audio at wall-clock speed. A passing result does not prove physical microphone/speaker behavior or portrait quality; verify those separately through `/avatar`.
