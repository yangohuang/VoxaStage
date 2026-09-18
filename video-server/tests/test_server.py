import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))

from flashhead_worker import _render_chunk_interruptibly, _render_events, create_app


JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9k="
)


class FakeEngine:
    width = 512
    height = 512
    fps = 25

    def __init__(self):
        self.calls = 0

    def start_clip(self):
        pass

    def render_chunk(self, _audio):
        self.calls += 1
        return [JPEG] * 24


def test_health_reports_readiness_and_active_lease():
    engine = FakeEngine()
    with TestClient(create_app(engine)) as client:
        response = client.get('/health')
        assert response.status_code == 503
        assert response.json()['busy'] is False
        engine.ready = True
        assert client.get('/health').status_code == 200
        with client.websocket_connect('/v1/stream') as ws:
            ws.send_json(dict(type='start', protocol=1, sample_rate=24000))
            assert ws.receive_json()['type'] == 'metadata'
            assert client.get('/health').json()['busy'] is True
            ws.send_json(dict(type='cancel'))
            assert ws.receive_json()['type'] == 'done'
        assert client.get('/health').json()['busy'] is False


def test_ws_protocol_emits_exact_real_audio_frame_count():
    """Contract test with FakeEngine; it does not exercise model inference."""
    app = create_app(FakeEngine())
    with TestClient(app) as client, client.websocket_connect("/v1/stream") as ws:
        ws.send_text(json.dumps({"type": "start", "protocol": 1, "sample_rate": 24000}))
        assert ws.receive_json()["type"] == "metadata"
        ws.send_bytes(b"\x00\x00" * 961)
        ws.send_text(json.dumps({"type": "end"}))
        frames = []
        while True:
            event = ws.receive_json()
            if event["type"] == "done":
                break
            frames.append(event)
        assert [event["index"] for event in frames] == [0, 1]
        assert [event["pts_seconds"] for event in frames] == [0.0, 0.04]
        assert all(base64.b64decode(event["image"]) == JPEG for event in frames)


def test_cancel_waits_for_running_inference_before_releasing_lease():
    """Contract test with FakeEngine; it proves lease cleanup is ordered."""
    class SlowEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            import threading
            self.started = threading.Event()
            self.finished = threading.Event()

        def render_chunk(self, audio):
            self.started.set()
            time.sleep(0.08)
            self.finished.set()
            return super().render_chunk(audio)

    async def scenario():
        engine = SlowEngine()
        cancelled = asyncio.Event()
        task = asyncio.create_task(_render_chunk_interruptibly(engine, np.zeros(15_360), cancelled))
        for _ in range(100):
            if engine.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert engine.started.is_set()
        cancelled.set()
        assert await task is None
        assert engine.finished.is_set()

    asyncio.run(scenario())


def test_cancel_is_read_even_when_pcm_queue_was_filled_during_inference():
    import threading
    class PausedEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
        def render_chunk(self, audio):
            self.started.set()
            assert self.release.wait(3)
            return super().render_chunk(audio)
    engine = PausedEngine()
    with TestClient(create_app(engine)) as client, client.websocket_connect('/v1/stream') as ws:
        ws.send_json(dict(type='start', protocol=1, sample_rate=24000))
        assert ws.receive_json()['type'] == 'metadata'
        ws.send_bytes(bytes(32000)); ws.send_bytes(bytes(32000))
        assert engine.started.wait(2)
        for _ in range(12):
            ws.send_bytes(bytes(12000))
        ws.send_json(dict(type='end'))
        ws.send_json(dict(type='cancel'))
        time.sleep(.05)  # Give receiver a turn while CUDA is deliberately gated.
        engine.release.set()
        events = []
        while True:
            event = ws.receive_json()
            events.append(event)
            if event['type'] in ('done', 'error'):
                break
        assert not any(event['type'] == 'frame' for event in events)
        assert engine.calls == 1


def test_output_failure_cancellation_wakes_renderer_waiting_for_audio():
    async def scenario():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        cancelled = asyncio.Event()
        task = asyncio.create_task(_render_events(incoming, outgoing, cancelled, FakeEngine()))
        await asyncio.sleep(0)
        cancelled.set()  # Same signal used by sender failure/disconnect.
        await asyncio.wait_for(task, .5)
    asyncio.run(scenario())


def test_repeated_task_cancellation_keeps_lease_until_inference_thread_finishes():
    import threading
    class GatedEngine(FakeEngine):
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()
        def render_chunk(self, audio):
            self.started.set()
            self.release.wait(2)
            self.finished.set()
            return [JPEG] * 24
    async def scenario():
        engine = GatedEngine()
        task = asyncio.create_task(_render_chunk_interruptibly(engine, np.zeros(15360), asyncio.Event()))
        try:
            for _ in range(100):
                if engine.started.is_set():
                    break
                await asyncio.sleep(.005)
            assert engine.started.is_set()
            task.cancel(); await asyncio.sleep(.01)
            task.cancel(); await asyncio.sleep(.01)
            assert not task.done(), 'A second cancellation released the model lease before CUDA finished'
        finally:
            engine.release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert engine.finished.is_set()
    asyncio.run(scenario())
