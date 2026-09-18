"""Bounded, full-duplex PCM16 WebSocket transport for stateful inference.

The sole model thread owns the admission slot through session.close(). Network
cancellation is cooperative: an in-flight model call must return before reuse.
"""
import asyncio
import base64
from dataclasses import asdict
import json
import logging
import math
import queue
import threading

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from serving.api import Cancelled, create_app

START_TIMEOUT_SECONDS = 10
MAX_PCM_BYTES = 32000
MAX_CONTROL_BYTES = 4096
INPUT_QUEUE_SIZE = 4
OUTPUT_QUEUE_SIZE = 8
POLL_SECONDS = 0.01
CONFIG_FIELDS = {'steps', 'seed', 'block_ms', 'lookahead_ms',
                 'audio_context_ms', 'history_frames'}


def _control(message):
    text = message.get('text')
    if text is None or len(text.encode('utf-8')) > MAX_CONTROL_BYTES:
        raise ValueError('Expected a JSON control message of at most 4096 bytes')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('Control message must be a JSON object')
    return data


def _config(data):
    from serving.incremental import StreamConfig
    if data.get('type') != 'start':
        raise ValueError('First message must have type=start')
    options = {key: value for key, value in data.items() if key != 'type'}
    if options.keys() - CONFIG_FIELDS:
        raise ValueError('Unknown start configuration field')
    if any(type(value) is not int for value in options.values()):
        raise ValueError('Configuration values must be integers')
    if not 1 <= options.get('steps', 10) <= 50:
        raise ValueError('steps must be between 1 and 50')
    if not 0 <= options.get('seed', 42) <= 2147483647:
        raise ValueError('seed must be between 0 and 2147483647')
    return StreamConfig(**options)


def _frame_event(frame):
    vertices = np.asarray(frame.vertices, dtype='<f4')
    if not np.isfinite(vertices).all() or not math.isfinite(frame.pts_seconds):
        raise ValueError('Model returned non-finite frame data')
    return {'type': 'frame', 'index': int(frame.index),
            'pts_seconds': float(frame.pts_seconds),
            'available_audio_samples': int(frame.available_audio_samples),
            'vertices': base64.b64encode(vertices.tobytes()).decode('ascii')}


def create_incremental_app(engine, session_factory=None):
    """Extend the legacy app so both endpoints share one inference reservation."""
    app = create_app(engine)

    @app.get('/v1/stream/info')
    async def stream_info():
        config = _config({'type': 'start'})
        return {'input_mode': 'incremental_pcm16', 'output_mode': 'streamed_vertices',
                'sample_rate': config.sample_rate, 'channels': 1, 'fps': config.fps,
                'busy': app.state.worker_lock.locked(), 'defaults': asdict(config),
                'max_steps': 50, 'max_pcm_bytes': MAX_PCM_BYTES,
                'resume_supported': False,
                'default_first_batch_audio_ms': config.block_ms + config.lookahead_ms,
                'default_future_audio_ms': {
                    'min': config.lookahead_ms + 1000 / config.fps,
                    'max': config.lookahead_ms + config.block_ms},
                'latency_note': 'Audio dependency only; compute, queues and network add latency'}

    @app.websocket('/v1/stream')
    async def stream(websocket: WebSocket):
        await websocket.accept()
        reserved = False
        worker_started = False
        terminal_claimed = False
        cancelled = threading.Event()
        tasks = []
        commands = queue.Queue(maxsize=INPUT_QUEUE_SIZE)
        events = queue.Queue(maxsize=OUTPUT_QUEUE_SIZE)

        async def send_terminal(event):
            nonlocal terminal_claimed
            if terminal_claimed:
                return
            # Claim before awaiting send: bytes can reach the peer before the
            # await completes, so writer.done() cannot identify this state.
            terminal_claimed = True
            await websocket.send_json(event)

        def emit(event):
            while not cancelled.is_set():
                try:
                    events.put(event, timeout=0.05)
                    return
                except queue.Full:
                    pass
            raise Cancelled()

        def worker(config):
            session = None
            terminal = None
            try:
                factory = session_factory
                if factory is None:
                    from serving.incremental import IncrementalSession
                    factory = IncrementalSession
                if cancelled.is_set():
                    return
                session = factory(engine, config)
                while not cancelled.is_set():
                    try:
                        kind, payload = commands.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if cancelled.is_set():
                        break
                    frames = session.finish() if kind == 'end' else session.push(payload)
                    for frame in frames:
                        emit(_frame_event(frame))
                    stats = session.stats()
                    if kind == 'end':
                        terminal = {'type': 'done', 'stats': stats}
                        break
                    emit({'type': 'progress', 'stats': stats})
            except Cancelled:
                pass
            except Exception as exc:
                logging.exception('Incremental inference failed')
                terminal = {'type': 'error', 'code': 'inference_error', 'message': str(exc)}
            finally:
                try:
                    if session is not None:
                        session.close()
                    if terminal is not None and terminal['type'] == 'done':
                        terminal['cleanup_complete'] = True
                except Exception as exc:
                    logging.exception('Incremental session cleanup failed')
                    terminal = {'type': 'error', 'code': 'cleanup_error', 'message': str(exc)}
                finally:
                    try:
                        if terminal is not None:
                            emit(terminal)
                    except Cancelled:
                        pass
                    finally:
                        app.state.worker_lock.release()

        async def enqueue(kind, payload=None):
            while not cancelled.is_set():
                try:
                    commands.put_nowait((kind, payload))
                    return
                except queue.Full:
                    await asyncio.sleep(POLL_SECONDS)
            raise asyncio.CancelledError()

        async def read_input():
            ended = False
            try:
                while True:
                    message = await websocket.receive()
                    if message['type'] == 'websocket.disconnect':
                        return None
                    if message.get('bytes') is not None:
                        if ended:
                            raise ValueError('Audio received after end')
                        payload = message['bytes']
                        if not payload or len(payload) % 2 or len(payload) > MAX_PCM_BYTES:
                            raise ValueError('PCM16 chunks must contain 1–16000 mono samples (2–32000 even bytes)')
                        samples = np.frombuffer(payload, dtype='<i2').astype(np.float32) / 32768.0
                        await enqueue('audio', samples)
                        continue
                    data = _control(message)
                    if data == {'type': 'cancel'}:
                        return {'type': 'cancelled'}
                    if data == {'type': 'end'} and not ended:
                        ended = True
                        await enqueue('end')
                        # Continue receiving to detect disconnect, cancellation,
                        # and invalid trailing input while the worker flushes.
                        continue
                    raise ValueError('Expected type=end or type=cancel; end is allowed only once')
            except (ValueError, TypeError) as exc:
                return {'type': 'error', 'code': 'invalid_input', 'message': str(exc)}
            except (WebSocketDisconnect, OSError):
                return None

        async def write_output():
            try:
                while True:
                    try:
                        event = events.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    if event['type'] in {'done', 'error'}:
                        await send_terminal(event)
                        return
                    await websocket.send_json(event)
            except (WebSocketDisconnect, OSError):
                return

        try:
            try:
                message = await asyncio.wait_for(websocket.receive(), START_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await send_terminal({'type': 'error', 'code': 'start_timeout',
                                           'message': 'Timed out waiting for type=start'})
                return
            if message['type'] == 'websocket.disconnect':
                return
            config = _config(_control(message))
            if not app.state.worker_lock.acquire(blocking=False):
                await send_terminal({'type': 'error', 'code': 'busy',
                                           'message': 'GPU worker busy; retry after current session'})
                return
            reserved = True
            await websocket.send_json({'type': 'metadata', 'fps': engine.fps,
                                       'faces': engine.faces.tolist(),
                                       'vertex_count': len(engine.template),
                                       'subject': engine.subject, 'dtype': '<f4',
                                       'sample_rate': 16000, 'channels': 1,
                                       'input_mode': 'incremental_pcm16',
                                       'config': asdict(config)})
            thread = threading.Thread(target=worker, args=(config,), daemon=True,
                                      name='incremental-inference')
            thread.start()
            worker_started = True
            reader = asyncio.create_task(read_input())
            writer = asyncio.create_task(write_output())
            tasks = [reader, writer]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                terminal = reader.result()
                cancelled.set()
                # Stop even an already-claimed terminal send: waiting for a
                # stalled peer here would leave no reader to notice disconnect.
                # send_terminal preserves at-most-once ownership after cancel.
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
                if terminal is not None:
                    await send_terminal(terminal)
            else:
                writer.result()
        except (ValueError, TypeError) as exc:
            await send_terminal({'type': 'error', 'code': 'invalid_start',
                                       'message': str(exc)})
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            cancelled.set()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if reserved and not worker_started:
                app.state.worker_lock.release()
            try:
                await websocket.close()
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass

    return app
