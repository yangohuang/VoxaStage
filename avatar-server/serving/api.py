"""A single-worker HTTP adapter with streamed mesh output."""
import asyncio
import base64
import io
import json
import logging
import math
import queue
import threading
import time

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from scipy.signal import resample_poly

MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 30


def decode_audio(payload):
    try:
        with sf.SoundFile(io.BytesIO(payload)) as f:
            if not 0.1 <= f.frames / f.samplerate <= MAX_SECONDS:
                raise ValueError(f'Audio must be 0.1–{MAX_SECONDS} seconds long')
            if not 8000 <= f.samplerate <= 192000 or f.channels > 8:
                raise ValueError('Unsupported audio sample rate or channel count')
            sr = f.samplerate
            samples = f.read(dtype='float32', always_2d=True).mean(axis=1)
        if not np.isfinite(samples).all():
            raise ValueError('Audio contains non-finite samples')
        if sr != 16000:
            divisor = math.gcd(sr, 16000)
            with np.errstate(over='ignore', invalid='ignore'):
                samples = resample_poly(samples, 16000 // divisor, sr // divisor)
        if not np.isfinite(samples).all():
            raise ValueError('Audio resampling produced non-finite samples')
        return np.ascontiguousarray(samples, dtype=np.float32)
    except (ValueError, RuntimeError, sf.LibsndfileError) as exc:
        raise HTTPException(400, str(exc)) from exc


class Cancelled(Exception):
    pass


class FrameSink:
    def __init__(self, events, cancelled):
        self.events = events
        self.cancelled = cancelled
        self.count = 0
        self.started = time.perf_counter()
        self.first_frame_seconds = None

    def emit(self, event):
        while not self.cancelled.is_set():
            try:
                self.events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue
        raise Cancelled()

    def put(self, vertices):
        vertices = np.asarray(vertices, dtype='<f4')
        if not np.isfinite(vertices).all():
            raise ValueError('Model returned non-finite vertices')
        if self.first_frame_seconds is None:
            self.first_frame_seconds = time.perf_counter() - self.started
        self.emit({'type': 'frame', 'index': self.count,
                   'vertices': base64.b64encode(vertices.tobytes()).decode('ascii')})
        self.count += 1


def create_app(engine):
    app = FastAPI(title='StreamingTalker', version='1.0')
    app.state.worker_lock = threading.Lock()

    @app.get('/health')
    def health():
        return {'status': 'ready', 'model': 'StreamingTalker-VOCASET',
                'busy': app.state.worker_lock.locked(), 'fps': engine.fps,
                'vertices': len(engine.template), 'subject': engine.subject,
                'max_audio_seconds': MAX_SECONDS,
                'input_mode': 'complete_audio', 'output_mode': 'streamed_vertices'}

    @app.post('/v1/animate')
    async def animate(request: Request, steps: int = Query(50, ge=1, le=100),
                      seed: int = Query(42, ge=0, le=2147483647)):
        if not app.state.worker_lock.acquire(blocking=False):
            raise HTTPException(409, 'GPU worker busy; retry after current request')
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise HTTPException(413, 'Audio upload exceeds 20 MiB')
            decoding = asyncio.create_task(asyncio.to_thread(decode_audio, bytes(body)))
            try:
                audio = await asyncio.shield(decoding)
            except asyncio.CancelledError:
                # Keep the admission slot until the CPU decoder actually finishes.
                await decoding
                raise
        except BaseException:
            app.state.worker_lock.release()
            raise

        events = queue.Queue(maxsize=8)
        cancelled = threading.Event()
        sink = FrameSink(events, cancelled)
        worker_started = False

        def worker():
            try:
                engine.generate(audio, steps, seed, sink)
                if sink.count == 0:
                    raise RuntimeError('Model generated no frames')
                sink.emit({'type': 'done', 'frames': sink.count,
                           'inference_seconds': time.perf_counter() - sink.started,
                           'first_frame_seconds': sink.first_frame_seconds,
                           'audio_seconds': len(audio) / 16000})
            except Cancelled:
                pass
            except Exception as exc:
                logging.exception('Inference failed')
                try:
                    sink.emit({'type': 'error', 'message': str(exc)})
                except Cancelled:
                    pass
            finally:
                app.state.worker_lock.release()

        async def stream():
            nonlocal worker_started
            try:
                threading.Thread(target=worker, daemon=True).start()
                worker_started = True
                yield json.dumps({'type': 'metadata', 'fps': engine.fps,
                                  'vertex_count': len(engine.template), 'dtype': '<f4',
                                  'faces': engine.faces.tolist(), 'subject': engine.subject,
                                  'steps': steps, 'seed': seed}) + '\n'
                while True:
                    try:
                        event = await asyncio.to_thread(events.get, True, 0.5)
                    except queue.Empty:
                        if await request.is_disconnected():
                            break
                        continue
                    yield json.dumps(event, allow_nan=False) + '\n'
                    if event['type'] in ('done', 'error'):
                        break
            finally:
                cancelled.set()

        class WorkerResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    cancelled.set()
                    if not worker_started:
                        app.state.worker_lock.release()

        return WorkerResponse(stream(), media_type='application/x-ndjson',
                              headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})

    return app
