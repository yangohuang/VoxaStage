"""CPU interface tests; these stubs do not validate model streaming quality."""
import asyncio
import base64
import importlib
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient


class Engine:
    fps = 30
    faces = np.array([[0, 1, 2]], dtype=np.int32)
    template = np.zeros((3, 3), dtype=np.float32)
    subject = 'test'


class Session:
    def __init__(self, engine, config):
        self.config = config
        self.samples = 0
        self.frames = 0
        self.closed = False
        self.thread_ids = [threading.get_ident()]

    def push(self, samples):
        self.thread_ids.append(threading.get_ident())
        assert samples.dtype == np.float32 and samples.ndim == 1
        self.samples += len(samples)
        frame = SimpleNamespace(index=self.frames, pts_seconds=self.frames / 30,
                                available_audio_samples=self.samples,
                                vertices=np.full((3, 3), self.frames + 0.25, np.float32))
        self.frames += 1
        return [frame]

    def finish(self):
        self.thread_ids.append(threading.get_ident())
        return []

    def stats(self):
        return {'frames': self.frames, 'received_samples': self.samples,
                'initialization_count': 1}

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.closed = True


def make_app(factory=Session):
    assert importlib.util.find_spec('serving.incremental_api') is not None, 'incremental transport is missing'
    module = importlib.import_module('serving.incremental_api')
    return module.create_incremental_app(Engine(), session_factory=factory)


def start(ws, **config):
    ws.send_json({'type': 'start', **config})
    return ws.receive_json()


def until_terminal(ws):
    events = []
    while True:
        events.append(ws.receive_json())
        if events[-1]['type'] in {'error', 'done', 'cancelled'}:
            return events


def test_first_frames_arrive_before_end_with_exact_indices_pts_and_pcm():
    sessions = []
    def factory(engine, config):
        session = Session(engine, config)
        sessions.append(session)
        return session
    with TestClient(make_app(factory)) as client:
        with client.websocket_connect('/v1/stream') as ws:
            metadata = start(ws, steps=4, seed=3)
            assert metadata['type'] == 'metadata'
            assert metadata['input_mode'] == 'incremental_pcm16'
            assert metadata['fps'] == 30 and metadata['vertex_count'] == 3
            assert metadata['faces'] == [[0, 1, 2]]
            assert metadata['config']['steps'] == 4
            for index in range(2):
                ws.send_bytes(np.zeros(3200, dtype='<i2').tobytes())
                frame = ws.receive_json()
                assert frame['type'] == 'frame' and frame['index'] == index
                assert frame['pts_seconds'] == index / 30
                assert frame['available_audio_samples'] == 3200 * (index + 1)
                vertices = np.frombuffer(base64.b64decode(frame['vertices']), dtype='<f4')
                np.testing.assert_allclose(vertices, index + 0.25)
                progress = ws.receive_json()
                assert progress['type'] == 'progress'
                assert progress['stats']['received_samples'] == 3200 * (index + 1)
            ws.send_json({'type': 'end'})
            done = until_terminal(ws)[-1]
            assert done['type'] == 'done' and done['stats']['frames'] == 2
    assert sessions[0].closed
    assert len(set(sessions[0].thread_ids)) == 1
    assert sessions[0].thread_ids[0] != threading.get_ident()


@pytest.mark.parametrize('payload', [b'', b'x', bytes(32002)], ids=['empty', 'odd', 'oversized'])
def test_invalid_pcm_is_terminal_and_releases_slot(payload):
    app = make_app()
    with TestClient(app) as client:
        for _ in range(2):
            with client.websocket_connect('/v1/stream') as ws:
                assert start(ws)['type'] == 'metadata'
                ws.send_bytes(payload)
                assert until_terminal(ws)[-1]['type'] == 'error'
            # The thread may still be unwinding when the terminal message arrives.
            import time
            deadline = time.monotonic() + 2
            while app.state.worker_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not app.state.worker_lock.locked()


@pytest.mark.parametrize('config', [{'steps': 0}, {'seed': -1}, {'block_ms': 0},
                                    {'steps': True}, {'unknown': 12}])
def test_invalid_config_is_terminal(config):
    with TestClient(make_app()) as client:
        with client.websocket_connect('/v1/stream') as ws:
            assert start(ws, **config)['type'] == 'error'
        assert client.get('/health').json()['busy'] is False


def test_busy_is_shared_with_legacy_http_and_health():
    with TestClient(make_app()) as client:
        with client.websocket_connect('/v1/stream') as first:
            assert start(first)['type'] == 'metadata'
            assert client.get('/health').json()['busy'] is True
            assert client.post('/v1/animate', content=b'invalid').status_code == 409
            with client.websocket_connect('/v1/stream') as second:
                assert start(second)['code'] == 'busy'
            first.send_json({'type': 'cancel'})
            assert until_terminal(first)[-1]['type'] == 'cancelled'


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), 3)


class SocketHarness:
    def __init__(self, app, block_frames=False, on_event=None):
        self.app = app
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({'type': 'websocket.connect'})
        self.events = []
        self.block_frames = block_frames
        self.on_event = on_event
        self.frame_send_started = asyncio.Event()
        self.never = asyncio.Event()

    async def run(self):
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                 'path': '/v1/stream', 'raw_path': b'/v1/stream', 'query_string': b'',
                 'headers': [], 'client': ('test', 1), 'server': ('test', 80),
                 'subprotocols': [], 'root_path': ''}
        async def send(message):
            if message['type'] == 'websocket.send':
                event = json.loads(message['text'])
                self.events.append(event)
                if self.on_event is not None:
                    await self.on_event(event)
                if self.block_frames and event['type'] == 'frame':
                    self.frame_send_started.set()
                    await self.never.wait()
        await self.app(scope, self.incoming.get, send)

    async def text(self, message):
        await self.incoming.put({'type': 'websocket.receive', 'text': json.dumps(message)})

    async def audio(self):
        await self.incoming.put({'type': 'websocket.receive', 'bytes': bytes(6400)})


def test_cancel_keeps_slot_until_blocking_model_work_and_close_finish():
    entered, release, closing, close_release = [threading.Event() for _ in range(4)]
    class BlockingSession(Session):
        def push(self, samples):
            entered.set()
            assert release.wait(5)
            return super().push(samples)
        def close(self):
            closing.set()
            assert close_release.wait(5)
            super().close()
    app = make_app(BlockingSession)
    async def scenario():
        socket = SocketHarness(app)
        task = asyncio.create_task(socket.run())
        try:
            await socket.text({'type': 'start'})
            await wait_until(lambda: socket.events)
            await socket.audio()
            await wait_until(entered.is_set)
            await socket.text({'type': 'cancel'})
            await asyncio.wait_for(task, 2)
            assert socket.events[-1]['type'] == 'cancelled'
            assert app.state.worker_lock.locked()
            release.set()
            await wait_until(closing.is_set)
            assert app.state.worker_lock.locked()
            close_release.set()
            await wait_until(lambda: not app.state.worker_lock.locked())
        finally:
            release.set()
            close_release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_disconnect_unblocks_full_output_queue_and_releases_worker():
    closed = threading.Event()
    class ManyFrames(Session):
        def push(self, samples):
            return [super(ManyFrames, self).push(samples)[0] for _ in range(100)]
        def close(self):
            super().close()
            closed.set()
    app = make_app(ManyFrames)
    async def scenario():
        socket = SocketHarness(app, block_frames=True)
        task = asyncio.create_task(socket.run())
        await socket.text({'type': 'start'})
        await socket.audio()
        await asyncio.wait_for(socket.frame_send_started.wait(), 2)
        await socket.incoming.put({'type': 'websocket.disconnect', 'code': 1000})
        await asyncio.wait_for(task, 2)
        await wait_until(closed.is_set)
        await wait_until(lambda: not app.state.worker_lock.locked())
    asyncio.run(scenario())


def test_audio_after_end_is_rejected_while_finish_is_running():
    entered, release = threading.Event(), threading.Event()
    class SlowFinish(Session):
        def finish(self):
            entered.set()
            assert release.wait(5)
            return []
    app = make_app(SlowFinish)
    async def scenario():
        socket = SocketHarness(app)
        task = asyncio.create_task(socket.run())
        try:
            await socket.text({'type': 'start'})
            await wait_until(lambda: socket.events)
            await socket.text({'type': 'end'})
            await wait_until(entered.is_set)
            await socket.audio()
            await asyncio.wait_for(task, 2)
            assert socket.events[-1]['type'] == 'error'
            assert 'after end' in socket.events[-1]['message']
        finally:
            release.set()
            await wait_until(lambda: not app.state.worker_lock.locked())
    asyncio.run(scenario())


def test_malformed_start_and_timeout_do_not_reserve_worker(monkeypatch):
    app = make_app()
    module = importlib.import_module('serving.incremental_api')
    monkeypatch.setattr(module, 'START_TIMEOUT_SECONDS', 0.03)
    with TestClient(app) as client:
        with client.websocket_connect('/v1/stream') as ws:
            ws.send_text('not json')
            assert ws.receive_json()['type'] == 'error'
        with client.websocket_connect('/v1/stream') as ws:
            assert ws.receive_json()['code'] == 'start_timeout'
        assert not app.state.worker_lock.locked()


def test_inference_error_is_terminal_and_next_connection_gets_fresh_state():
    closed = threading.Event()
    class FailingSession(Session):
        def push(self, samples):
            raise RuntimeError('stub failure')
        def close(self):
            closed.set()
    app = make_app(FailingSession)
    with TestClient(app) as client:
        for _ in range(2):
            with client.websocket_connect('/v1/stream') as ws:
                assert start(ws)['type'] == 'metadata'
                ws.send_bytes(bytes(6400))
                result = until_terminal(ws)[-1]
                assert result['type'] == 'error' and 'stub failure' in result['message']
            assert closed.wait(2)
    assert not app.state.worker_lock.locked()


def test_input_queue_applies_backpressure_without_blocking_event_loop():
    entered, release = threading.Event(), threading.Event()
    class BlockingSession(Session):
        def push(self, samples):
            entered.set()
            assert release.wait(5)
            return super().push(samples)
    app = make_app(BlockingSession)
    module = importlib.import_module('serving.incremental_api')
    async def scenario():
        socket = SocketHarness(app)
        task = asyncio.create_task(socket.run())
        try:
            await socket.text({'type': 'start'})
            await wait_until(lambda: socket.events)
            for _ in range(20):
                await socket.audio()
            await wait_until(entered.is_set)
            await asyncio.sleep(0.05)
            # One push is executing, at most N are queued, and the reader can
            # hold one additional chunk while waiting for queue capacity.
            assert socket.incoming.qsize() >= 20 - module.INPUT_QUEUE_SIZE - 2
            assert app.state.worker_lock.locked()
            release.set()
            await socket.text({'type': 'end'})
            await asyncio.wait_for(task, 3)
            assert socket.events[-1]['type'] == 'done'
            assert socket.events[-1]['stats']['received_samples'] == 20 * 3200
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['init', 'close'])
def test_session_lifecycle_failures_report_terminal_error_and_release(phase):
    class BrokenLifecycle(Session):
        def __init__(self, engine, config):
            if phase == 'init':
                raise ValueError('constructor failure')
            super().__init__(engine, config)
        def close(self):
            raise RuntimeError('cleanup failure')
    app = make_app(BrokenLifecycle)
    with TestClient(app) as client:
        with client.websocket_connect('/v1/stream') as ws:
            assert start(ws)['type'] == 'metadata'
            if phase == 'close':
                ws.send_json({'type': 'end'})
            assert until_terminal(ws)[-1]['type'] == 'error'
    assert not app.state.worker_lock.locked()


def test_fresh_session_restarts_indices_and_input_counts_after_completion():
    sessions = []
    def factory(engine, config):
        session = Session(engine, config)
        sessions.append(session)
        return session
    with TestClient(make_app(factory)) as client:
        for _ in range(2):
            with client.websocket_connect('/v1/stream') as ws:
                assert start(ws)['type'] == 'metadata'
                ws.send_bytes(bytes(6400))
                assert ws.receive_json()['index'] == 0
                assert ws.receive_json()['stats']['received_samples'] == 3200
                ws.send_json({'type': 'end'})
                assert until_terminal(ws)[-1]['type'] == 'done'
    assert len(sessions) == 2 and all(s.closed for s in sessions)


def test_slow_consumer_bounds_encoded_output_then_cancel_unblocks(monkeypatch):
    class ManyFrames(Session):
        def push(self, samples):
            return [super(ManyFrames, self).push(samples)[0] for _ in range(100)]
    app = make_app(ManyFrames)
    module = importlib.import_module('serving.incremental_api')
    encoded = []
    original = module._frame_event
    def observe(frame):
        encoded.append(frame.index)
        return original(frame)
    monkeypatch.setattr(module, '_frame_event', observe)
    async def scenario():
        socket = SocketHarness(app, block_frames=True)
        task = asyncio.create_task(socket.run())
        await socket.text({'type': 'start'})
        await socket.audio()
        await asyncio.wait_for(socket.frame_send_started.wait(), 2)
        await asyncio.sleep(0.1)
        assert len(encoded) <= module.OUTPUT_QUEUE_SIZE + 2
        await socket.text({'type': 'cancel'})
        await asyncio.wait_for(task, 2)
        assert socket.events[-1]['type'] == 'cancelled'
        await wait_until(lambda: not app.state.worker_lock.locked())
    asyncio.run(scenario())


@pytest.mark.parametrize('send_delay', [0, 0.02], ids=['same_turn', 'send_in_flight'])
@pytest.mark.parametrize('trailing', [{'type': 'cancel'}, {'type': 'end'}],
                         ids=['cancel', 'invalid_control'])
def test_worker_terminal_cannot_be_followed_by_reader_terminal(send_delay, trailing):
    app = make_app()
    async def scenario():
        async def inject_control_during_done(event):
            if event['type'] == 'done':
                await socket.text(trailing)
                # The done message has reached ASGI send, but send_json has
                # not returned. Checking only writer.done() misses this race.
                await asyncio.sleep(send_delay)
        socket = SocketHarness(app, on_event=inject_control_during_done)
        task = asyncio.create_task(socket.run())
        await socket.text({'type': 'start'})
        await socket.text({'type': 'end'})
        await asyncio.wait_for(task, 2)
        assert [event['type'] for event in socket.events] == ['metadata', 'done']
        await wait_until(lambda: not app.state.worker_lock.locked())
    asyncio.run(scenario())


def test_steps_limit_message_matches_adapter():
    with TestClient(make_app()) as client:
        with client.websocket_connect('/v1/stream') as ws:
            error = start(ws, steps=51)
            assert error['type'] == 'error'
            assert error['message'] == 'steps must be between 1 and 50'


def test_stream_info_reports_incremental_capabilities_without_changing_health():
    with TestClient(make_app()) as client:
        response = client.get('/v1/stream/info')
        assert response.status_code == 200
        info = response.json()
        assert info['input_mode'] == 'incremental_pcm16'
        assert info['sample_rate'] == 16000 and info['fps'] == 30
        assert info['defaults']['block_ms'] == info['defaults']['lookahead_ms'] == 200
        assert info['default_first_batch_audio_ms'] == 400
        assert info['default_future_audio_ms']['min'] == pytest.approx(233.333333)
        assert info['default_future_audio_ms']['max'] == 400
        assert info['max_steps'] == 50 and info['resume_supported'] is False
        assert info['busy'] is False
        assert client.get('/health').json()['input_mode'] == 'complete_audio'
        with client.websocket_connect('/v1/stream') as ws:
            assert start(ws)['type'] == 'metadata'
            assert client.get('/v1/stream/info').json()['busy'] is True
            ws.send_json({'type': 'cancel'})
            assert until_terminal(ws)[-1]['type'] == 'cancelled'


def test_done_marks_cleanup_complete_and_preserves_generation_end_stats():
    class HistorySession(Session):
        def stats(self):
            return {**super().stats(), 'closed': self.closed,
                    'latent_history_frames': 0 if self.closed else 4}
    with TestClient(make_app(HistorySession)) as client:
        with client.websocket_connect('/v1/stream') as ws:
            assert start(ws)['type'] == 'metadata'
            ws.send_json({'type': 'end'})
            done = until_terminal(ws)[-1]
            assert done['type'] == 'done'
            assert done['cleanup_complete'] is True
            assert done['stats']['closed'] is False
            assert done['stats']['latent_history_frames'] == 4


def test_cancel_then_disconnect_stops_a_claimed_terminal_send_that_never_returns():
    app = make_app()
    async def scenario():
        terminal_send_started = asyncio.Event()
        never = asyncio.Event()
        async def block_done(event):
            if event['type'] == 'done':
                terminal_send_started.set()
                await never.wait()
        socket = SocketHarness(app, on_event=block_done)
        task = asyncio.create_task(socket.run())
        try:
            await socket.text({'type': 'start'})
            await socket.text({'type': 'end'})
            await asyncio.wait_for(terminal_send_started.wait(), 2)
            await socket.text({'type': 'cancel'})
            await wait_until(socket.incoming.empty)
            await socket.incoming.put({'type': 'websocket.disconnect', 'code': 1000})
            await asyncio.wait_for(task, 0.5)
            assert [event['type'] for event in socket.events] == ['metadata', 'done']
            assert not app.state.worker_lock.locked()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
