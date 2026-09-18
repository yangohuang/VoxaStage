"""Versioned WebSocket bridge for the upstream SoulX-FlashHead Lite pipeline.

The wire protocol deliberately transports original 24 kHz PCM separately from
generated JPEG frames.  FlashHead receives a continuous 16 kHz resample only.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import importlib
import io
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from PIL import Image


INPUT_SAMPLE_RATE = 24_000
MODEL_SAMPLE_RATE = 16_000
FPS = 25
INPUT_FRAME_SAMPLES = INPUT_SAMPLE_RATE // FPS
MODEL_CHUNK_SAMPLES = 24 * MODEL_SAMPLE_RATE // FPS  # Lite emits 24 new frames.
MAX_AUDIO_SAMPLES = 30 * INPUT_SAMPLE_RATE
MAX_INPUT_MESSAGE_BYTES = 32_000
OUTPUT_QUEUE_SIZE = 8
INPUT_QUEUE_SIZE = 512
SEND_TIMEOUT_SECONDS = 10
INITIAL_MESSAGE_TIMEOUT_SECONDS = 10
MAX_CONTROL_MESSAGE_BYTES = 4_096
logger = logging.getLogger(__name__)


class Engine(Protocol):
    width: int
    height: int
    fps: int

    def start_clip(self) -> None: ...
    def render_chunk(self, audio: np.ndarray) -> list[bytes]: ...


class AudioChunker:
    """Continuous 24 kHz PCM input to padded 0.96 s FlashHead chunks.

    Frame accounting intentionally comes from input PCM, never resample padding:
    the client therefore plays exactly the original audio duration.
    """

    def __init__(self) -> None:
        try:
            import soxr
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("soxr is required for the FlashHead worker") from exc
        self._stream = soxr.ResampleStream(INPUT_SAMPLE_RATE, MODEL_SAMPLE_RATE, 1, dtype="float32")
        self._resampled = np.empty(0, dtype=np.float32)
        self._finished = False
        self.total_input_samples = 0

    @property
    def expected_frame_count(self) -> int:
        return (self.total_input_samples + INPUT_FRAME_SAMPLES - 1) // INPUT_FRAME_SAMPLES

    def feed(self, pcm: bytes) -> None:
        if self._finished:
            raise ValueError("audio already ended")
        if not pcm or len(pcm) % 2:
            raise ValueError("PCM16LE payload must contain an even, non-empty byte count")
        samples = len(pcm) // 2
        if self.total_input_samples + samples > MAX_AUDIO_SAMPLES:
            raise ValueError("audio exceeds the 30 seconds limit")
        self.total_input_samples += samples
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._append_resampled(self._stream.resample_chunk(audio, last=False))

    def ready_model_chunks(self) -> Iterable[np.ndarray]:
        while len(self._resampled) >= MODEL_CHUNK_SAMPLES:
            chunk, self._resampled = self._resampled[:MODEL_CHUNK_SAMPLES], self._resampled[MODEL_CHUNK_SAMPLES:]
            yield chunk

    def finish_model_chunks(self) -> Iterable[np.ndarray]:
        if not self._finished:
            self._finished = True
            self._append_resampled(self._stream.resample_chunk(np.empty(0, dtype=np.float32), last=True))
        yield from self.ready_model_chunks()
        if len(self._resampled):
            padded = np.pad(self._resampled, (0, MODEL_CHUNK_SAMPLES - len(self._resampled)))
            self._resampled = np.empty(0, dtype=np.float32)
            yield padded.astype(np.float32, copy=False)

    def _append_resampled(self, output: np.ndarray) -> None:
        if len(output):
            self._resampled = np.concatenate((self._resampled, np.asarray(output, dtype=np.float32)))


@dataclass(frozen=True)
class FlashHeadConfig:
    source: Path
    checkpoint: Path
    wav2vec: Path
    portrait: Path
    seed: int = 9999
    use_face_crop: bool = False
    enable_torch_compile: bool = False


class FlashHeadEngine:
    """Lazy adapter around the pinned upstream pipeline; no upstream files change."""

    width = 512
    height = 512
    fps = FPS

    def __init__(self, config: FlashHeadConfig) -> None:
        self.config = config
        self._load_lock = threading.Lock()
        self._loaded = False

    @property
    def ready(self) -> bool:
        return self._loaded

    def start_clip(self) -> None:
        self._ensure_loaded()
        self.pipeline.reset_person_name(self._person_name)
        self._history: deque[float] = deque([0.0] * (8 * MODEL_SAMPLE_RATE), maxlen=8 * MODEL_SAMPLE_RATE)

    def render_chunk(self, audio: np.ndarray) -> list[bytes]:
        started = time.perf_counter()
        self._history.extend(np.asarray(audio, dtype=np.float32).tolist())
        audio_array = np.asarray(self._history, dtype=np.float32)
        embedding = self.inference.get_audio_embedding(
            self.pipeline, audio_array, self._audio_start_idx, self._audio_end_idx
        )
        video = self.inference.run_pipeline(self.pipeline, embedding)[self._motion_frames_num :]
        frames = video.detach().cpu().numpy().astype(np.uint8, copy=False)
        images = [_jpeg(frame) for frame in frames]
        self.last_chunk_seconds = time.perf_counter() - started
        return images

    def diagnostics(self) -> dict:
        if not self._loaded:
            return {}
        import torch
        mib = 1024 ** 2
        return dict(gpu_allocated_mib=round(torch.cuda.memory_allocated() / mib, 1),
                    gpu_reserved_mib=round(torch.cuda.memory_reserved() / mib, 1),
                    gpu_peak_allocated_mib=round(torch.cuda.max_memory_allocated() / mib, 1),
                    last_chunk_seconds=getattr(self, 'last_chunk_seconds', None))

    def _ensure_loaded(self) -> None:
        with self._load_lock:
            if self._loaded:
                return
            if not self.config.source.is_dir():
                raise RuntimeError(f"FlashHead source is not a directory: {self.config.source}")
            for label, path in (("checkpoint", self.config.checkpoint), ("wav2vec", self.config.wav2vec), ("portrait", self.config.portrait)):
                if not path.exists():
                    raise RuntimeError(f"FlashHead {label} does not exist: {path}")
            sys.path.insert(0, str(self.config.source))
            # Upstream inference.py reads its YAML using a relative path at import.
            previous_cwd = os.getcwd()
            try:
                os.chdir(self.config.source)
                self.inference = importlib.import_module("flash_head.inference")
                pipeline_module = importlib.import_module("flash_head.src.pipeline.flash_head_pipeline")
            finally:
                os.chdir(previous_cwd)
            pipeline_module.COMPILE_MODEL = self.config.enable_torch_compile
            pipeline_module.COMPILE_VAE = self.config.enable_torch_compile
            self.pipeline = self.inference.get_pipeline(
                world_size=1,
                ckpt_dir=str(self.config.checkpoint),
                model_type="lite",
                wav2vec_dir=str(self.config.wav2vec),
            )
            self.inference.get_base_data(
                self.pipeline,
                cond_image_path_or_dir=str(self.config.portrait),
                base_seed=self.config.seed,
                use_face_crop=self.config.use_face_crop,
            )
            params = self.inference.get_infer_params()
            if params["sample_rate"] != MODEL_SAMPLE_RATE or params["tgt_fps"] != FPS:
                raise RuntimeError("unexpected upstream Lite audio or FPS configuration")
            self._motion_frames_num = params["motion_frames_num"]
            self._audio_end_idx = params["cached_audio_duration"] * FPS
            self._audio_start_idx = self._audio_end_idx - params["frame_num"]
            self.width, self.height, self.fps = params["width"], params["height"], params["tgt_fps"]
            self._person_name = self.config.portrait.stem
            self._loaded = True


def _jpeg(frame: np.ndarray) -> bytes:
    image = Image.fromarray(frame, mode="RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90, optimize=False)
    return out.getvalue()


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="FlashHead Lite streaming worker")
    # This flag is deliberately checked before receiving a model lease.  Waiting
    # clients would otherwise accumulate audio while a CUDA call is uninterruptible.
    inference_lock = asyncio.Lock()

    @app.get("/health")
    async def health() -> JSONResponse:
        ready = bool(getattr(engine, "ready", False))
        diagnostics = engine.diagnostics() if ready and hasattr(engine, 'diagnostics') else {}
        return JSONResponse({"ok": ready, "ready": ready, "busy": inference_lock.locked(),
                             "engine": type(engine).__name__, **diagnostics}, status_code=200 if ready else 503)

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        cancelled = asyncio.Event()
        incoming: asyncio.Queue[tuple[str, bytes | None]] = asyncio.Queue(maxsize=INPUT_QUEUE_SIZE)
        receive_errors: list[Exception] = []
        outgoing: asyncio.Queue[dict] = asyncio.Queue(maxsize=OUTPUT_QUEUE_SIZE)
        receiver: asyncio.Task[None] | None = None
        sender: asyncio.Task[None] | None = None
        send_done = False
        lease_acquired = False
        try:
            start = await _receive_start(ws)
            _validate_start(start)
            if inference_lock.locked():
                await _send_error(ws, "busy")
                return
            # Lock.acquire completes without yielding while unlocked, so no
            # connection is queued behind a running clip.
            await inference_lock.acquire()
            lease_acquired = True
            receiver = asyncio.create_task(_receive_events(ws, incoming, cancelled, receive_errors), name="flashhead-receiver")
            if not await _start_clip_interruptibly(engine, cancelled):
                if receive_errors:
                    raise receive_errors[0]
                send_done = True
                return
            await _send_json(ws, _metadata(engine))
            sender = asyncio.create_task(_send_events(ws, outgoing, cancelled), name="flashhead-sender")
            await _render_events(incoming, outgoing, cancelled, engine)
            if receive_errors:
                raise receive_errors[0]
            if not cancelled.is_set():
                await outgoing.join()
            # This is an intentional terminal acknowledgement after the renderer
            # has stopped (including cancel), never an unconditional finally send.
            send_done = True
        except WebSocketDisconnect:
            cancelled.set()
        except ValueError as exc:
            logger.info("FlashHead protocol error: %s", exc)
            await _send_error(ws, "invalid_request")
        except RuntimeError:
            logger.exception("FlashHead model worker error")
            await _send_error(ws, "model_failure")
        except Exception:
            logger.exception("Unhandled FlashHead worker error")
            await _send_error(ws, "server_error")
        finally:
            cancelled.set()
            if receiver:
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await receiver
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await sender
            if lease_acquired:
                inference_lock.release()
            if send_done:
                with contextlib.suppress(Exception):
                    await _send_json(ws, {"type": "done", "cleanup_complete": True})

    return app


async def _receive_start(ws: WebSocket) -> dict:
    message = await asyncio.wait_for(ws.receive(), timeout=INITIAL_MESSAGE_TIMEOUT_SECONDS)
    if message.get("type") != "websocket.receive" or message.get("text") is None:
        raise ValueError("first message must be JSON start")
    if len(message["text"].encode("utf-8")) > MAX_CONTROL_MESSAGE_BYTES:
        raise ValueError("start message exceeds control size limit")
    try:
        event = json.loads(message["text"])
    except json.JSONDecodeError as exc:
        raise ValueError("start message is not valid JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("start message must be a JSON object")
    return event


def _validate_start(event: dict) -> None:
    if (event.get("type") != "start" or type(event.get("protocol")) is not int or event.get("protocol") != 1
            or type(event.get("sample_rate")) is not int or event.get("sample_rate") != INPUT_SAMPLE_RATE):
        raise ValueError("expected start with protocol=1 and sample_rate=24000")


def _metadata(engine: Engine) -> dict:
    return {"type": "metadata", "protocol": 1, "kind": "2d", "codec": "jpeg", "sample_rate": INPUT_SAMPLE_RATE,
            "fps": engine.fps, "width": engine.width, "height": engine.height}


async def _receive_events(ws: WebSocket, incoming: asyncio.Queue, cancelled: asyncio.Event,
                          errors: list[Exception]) -> None:
    total_bytes = 0
    ended = False
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                cancelled.set()
                return
            data = message.get("bytes")
            if data is not None:
                if ended or not data or len(data) % 2 or len(data) > MAX_INPUT_MESSAGE_BYTES:
                    raise ValueError("invalid PCM message or audio after end")
                total_bytes += len(data)
                if total_bytes > MAX_AUDIO_SAMPLES * 2:
                    raise ValueError("audio exceeds the 30 seconds limit")
                incoming.put_nowait(("pcm", data))
                continue
            text = message.get("text") or ""
            if len(text.encode("utf-8")) > MAX_CONTROL_MESSAGE_BYTES:
                raise ValueError("control message exceeds size limit")
            event = json.loads(text)
            if not isinstance(event, dict):
                raise ValueError("control message must be a JSON object")
            kind = event.get("type")
            if kind in {"end", "cancel"}:
                if kind == "cancel":
                    cancelled.set()
                    return
                if ended:
                    raise ValueError("duplicate end message")
                ended = True
                incoming.put_nowait((kind, None))
                # Keep receiving after end so disconnect/cancel can interrupt a
                # slow final CUDA call before its result is sent.
                continue
            raise ValueError("expected PCM, end, or cancel")
    except WebSocketDisconnect:
        cancelled.set()
    except (ValueError, asyncio.QueueFull) as exc:
        errors.append(ValueError("input buffer full" if isinstance(exc, asyncio.QueueFull) else str(exc)))
        cancelled.set()
    except Exception as exc:
        errors.append(exc)
        cancelled.set()


async def _render_events(incoming: asyncio.Queue, outgoing: asyncio.Queue, cancelled: asyncio.Event, engine: Engine) -> None:
    chunker = AudioChunker()
    frame_index = 0
    emitted = 0
    while True:
        item = await _get_or_cancel(incoming, cancelled)
        if item is None:
            return
        kind, payload = item
        if kind == "cancel":
            return
        if kind == "error":
            raise ValueError((payload or b"invalid input").decode())
        if kind == "pcm":
            chunker.feed(payload or b"")
            model_chunks = chunker.ready_model_chunks()
        elif kind == "end":
            model_chunks = chunker.finish_model_chunks()
        else:  # pragma: no cover - receiver owns event vocabulary
            raise ValueError("unknown receiver event")
        for model_chunk in model_chunks:
            frames = await _render_chunk_interruptibly(engine, model_chunk, cancelled)
            if frames is None:
                return
            for frame in frames:
                if cancelled.is_set():
                    return
                if emitted >= chunker.expected_frame_count:
                    break
                if not await _queue_event(outgoing, {"type": "frame", "index": frame_index, "pts_seconds": frame_index / FPS,
                                                     "image": base64.b64encode(frame).decode("ascii")}, cancelled):
                    return
                emitted += 1
                frame_index += 1
        if kind == "end":
            if emitted != chunker.expected_frame_count:
                raise RuntimeError("FlashHead returned fewer frames than required by input PCM")
            return


async def _render_chunk_interruptibly(engine: Engine, audio: np.ndarray, cancelled: asyncio.Event) -> list[bytes] | None:
    render = asyncio.create_task(asyncio.to_thread(engine.render_chunk, audio), name="flashhead-inference")
    result = await _await_thread_with_cleanup(render, cancelled, "flashhead-cancel-watch")
    if result is _CANCELLED:
        return None
    return result


async def _start_clip_interruptibly(engine: Engine, cancelled: asyncio.Event) -> bool:
    """Run load/reset outside the event loop and retain its lease on cancellation."""
    start = asyncio.create_task(asyncio.to_thread(engine.start_clip), name="flashhead-start-clip")
    return (await _await_thread_with_cleanup(start, cancelled, "flashhead-start-cancel-watch")) is not _CANCELLED


_CANCELLED = object()


async def _await_thread_with_cleanup(work: asyncio.Task, cancelled: asyncio.Event, watcher_name: str):
    """Never release the model lease while a non-cancellable CUDA thread runs."""
    watcher = asyncio.create_task(cancelled.wait(), name=watcher_name)
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            with contextlib.suppress(Exception):
                await asyncio.shield(work)
            return _CANCELLED
        return await work
    except asyncio.CancelledError:
        # ASGI task cancellation is distinct from the protocol cancel message.
        # A to_thread CUDA call keeps running, so wait for it before callers can
        # release the single inference lock or begin a later clip.
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # A second disconnect/shutdown cancellation must not detach
                # the still-running CUDA thread from its exclusive lease.
                continue
            except Exception:
                break
        if not work.cancelled():
            with contextlib.suppress(Exception):
                work.result()
        raise
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def _send_json(ws: WebSocket, event: dict) -> None:
    await asyncio.wait_for(ws.send_json(event), timeout=SEND_TIMEOUT_SECONDS)


async def _queue_event(outgoing: asyncio.Queue, event: dict, cancelled: asyncio.Event) -> bool:
    put = asyncio.create_task(outgoing.put(event))
    stop = asyncio.create_task(cancelled.wait())
    try:
        done, _ = await asyncio.wait((put, stop), return_when=asyncio.FIRST_COMPLETED)
        return stop not in done
    finally:
        put.cancel()
        stop.cancel()
        await asyncio.gather(put, stop, return_exceptions=True)


async def _get_or_cancel(incoming: asyncio.Queue, cancelled: asyncio.Event):
    get = asyncio.create_task(incoming.get())
    stop = asyncio.create_task(cancelled.wait())
    try:
        done, _ = await asyncio.wait((get, stop), return_when=asyncio.FIRST_COMPLETED)
        return None if stop in done else get.result()
    finally:
        get.cancel()
        stop.cancel()
        await asyncio.gather(get, stop, return_exceptions=True)


async def _send_events(ws: WebSocket, outgoing: asyncio.Queue, cancelled: asyncio.Event) -> None:
    while True:
        event = await outgoing.get()
        try:
            if cancelled.is_set():
                _discard_outgoing(outgoing)
                return
            await _send_json(ws, event)
        except Exception:
            cancelled.set()
            _discard_outgoing(outgoing)
            return
        finally:
            outgoing.task_done()


def _discard_outgoing(outgoing: asyncio.Queue) -> None:
    while True:
        try:
            outgoing.get_nowait()
        except asyncio.QueueEmpty:
            return
        else:
            outgoing.task_done()


async def _send_error(ws: WebSocket, code: str) -> None:
    with contextlib.suppress(Exception):
        await _send_json(ws, {"type": "error", "code": code})


def main() -> None:
    parser = argparse.ArgumentParser(description="FlashHead Lite PCM-to-JPEG streaming worker")
    parser.add_argument("--source", required=True, type=Path, help="pinned SoulX-FlashHead source checkout")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--wav2vec", required=True, type=Path)
    parser.add_argument("--portrait", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8203)
    parser.add_argument("--seed", type=int, default=9999)
    parser.add_argument("--use-face-crop", action="store_true")
    parser.add_argument("--enable-torch-compile", action="store_true", help="disabled by default for first validation")
    parser.add_argument("--lazy-load", action="store_true", help="defer model loading until the first start (useful for tests)")
    args = parser.parse_args()
    config = FlashHeadConfig(args.source.resolve(), args.checkpoint.resolve(), args.wav2vec.resolve(), args.portrait.resolve(),
                             args.seed, args.use_face_crop, args.enable_torch_compile)
    import uvicorn
    engine = FlashHeadEngine(config)
    if not args.lazy_load:
        engine.start_clip()
    uvicorn.run(create_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
