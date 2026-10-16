"""Protocol tests use synthetic waveforms, not a substitute for model acceptance."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np
import soxr
from fastapi.testclient import TestClient

import tts_runtime as runtime
from tts_server import create_app, parse_messages


def messages(voice='male', text='介绍一下语音 agent 技术。'):
    common = dict(session_id='session', transaction_id='turn', audio_id=voice, audio_sr=24000)
    return [dict(common, **part) for part in
            [{'open_stream': 1}, {'is_start': 1}, {'text': text}, {'is_end': 1}]]


class RequestTests(unittest.TestCase):
    def test_existing_application_contract(self):
        self.assertEqual(parse_messages(messages(), {'male': Path('/voice.wav')}),
                         ('male', '介绍一下语音 agent 技术。'))

    def test_rejects_wrong_voice_sequence_identity_rate_and_limits(self):
        cases = []
        for key, value in [('audio_id', '../voice.wav'), ('audio_sr', 22050),
                           ('session_id', 'other'), ('transaction_id', 'other')]:
            item = messages(); item[2][key] = value; cases.append(item)
        item = messages(); item[0], item[1] = item[1], item[0]; cases.append(item)
        item = messages(); item[2]['text'] = 'a' * 1001; cases.append(item)
        item = messages(); item[2]['text'] = ' '; cases.append(item)
        item = messages(); item[3]['text'] = 'late text'; cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_messages(item, {'male': Path('/voice.wav')})

    def test_voice_manifest_validates_actual_wave_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); audio = root / 'voice.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\x01\x00' * 24000)
            manifest = root / 'voices.json'
            row = {'id': 'male', 'wav_local_path': str(audio)}
            manifest.write_text(json.dumps([row]))
            self.assertEqual(runtime.load_voices(manifest), {'male': audio})
            manifest.write_text(json.dumps([row, row]))
            with self.assertRaises(ValueError): runtime.load_voices(manifest)
            audio.write_text('not audio')
            manifest.write_text(json.dumps([row]))
            with self.assertRaises(ValueError): runtime.load_voices(manifest)


class AudioTests(unittest.TestCase):
    def test_stream_resampling_preserves_length_signal_and_tail(self):
        source = (12000 * np.sin(np.arange(22051) * 0.07)).astype(np.float32)
        converter = runtime.PCMStream()
        packets = []
        for chunk in np.array_split(source, 19): packets.extend(converter.feed(chunk))
        packets.extend(converter.finish())
        actual = np.frombuffer(b''.join(packets), '<i2')
        expected = np.clip(np.rint(soxr.resample(source, 22050, 24000)), -32768, 32767).astype('<i2')
        self.assertEqual(len(actual), len(expected))
        self.assertLessEqual(np.max(np.abs(actual.astype(float) - expected)), 1)
        self.assertTrue(all(0 < len(p) <= 1920 and len(p) % 2 == 0 for p in packets))

    def test_nonfinite_stereo_or_empty_audio_is_not_silently_accepted(self):
        for bad in [np.array([np.nan]), np.zeros((2, 100)), np.array([])]:
            with self.subTest(shape=bad.shape), self.assertRaises(ValueError):
                list(runtime.PCMStream().feed(bad))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stops_audio_sender_before_reporting_error(self):
        engine = runtime.Engine(lambda *_: iter([np.ones(22050, dtype=np.float32)]),
                                {'male': Path('/male.wav')})
        app = create_app(engine, generation_timeout=.05)
        incoming = asyncio.Queue()
        incoming.put_nowait({'type': 'websocket.connect'})
        for item in messages():
            incoming.put_nowait({'type': 'websocket.receive', 'text': json.dumps(item)})
        sender_stopped = asyncio.Event(); errors = []
        async def send(event):
            if event.get('bytes') is not None:
                try: await asyncio.Event().wait()  # stalled peer applies send backpressure
                finally: sender_stopped.set()
            if event.get('text'):
                payload = json.loads(event['text'])
                if payload.get('code') == 504:
                    errors.append((engine.jobs[-1].stop.is_set(), sender_stopped.is_set()))
                    await asyncio.sleep(0)
        scope = {'type': 'websocket', 'path': '/ws/tts', 'raw_path': b'/ws/tts',
                 'query_string': b'', 'headers': [], 'scheme': 'ws', 'root_path': '',
                 'asgi': {'version': '3.0', 'spec_version': '2.4'}}
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, incoming.get, send), 2)
        self.assertEqual(errors, [(True, True)])

    async def test_audio_arrives_before_generator_finishes(self):
        release = threading.Event(); finalized = threading.Event()
        def generate(voice, text):
            try:
                yield np.ones(22050, dtype=np.float32)
                release.wait(3)
                yield np.ones(22050, dtype=np.float32)
            finally: finalized.set()
        engine = runtime.Engine(generate, {'male': Path('/voice.wav')})
        job = engine.start('male', 'hello')
        try:
            first = await asyncio.wait_for(job.receive(), 1)
            self.assertIsInstance(first, bytes)
            self.assertFalse(finalized.is_set())
            self.assertTrue(engine.busy)
        finally:
            job.cancel(); release.set(); await engine.shutdown()
        self.assertTrue(finalized.is_set()); self.assertFalse(engine.busy)

    async def test_cancel_keeps_model_owned_until_inflight_call_returns(self):
        entered = threading.Event(); release = threading.Event(); finalized = threading.Event()
        def generate(voice, text):
            try:
                entered.set(); release.wait(3)
                yield np.ones(22050, dtype=np.float32)
            finally: finalized.set()
        engine = runtime.Engine(generate, {'male': Path('/voice.wav')})
        job = engine.start('male', 'hello')
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            job.cancel()
            self.assertTrue(engine.busy)
            with self.assertRaises(runtime.BusyError): engine.start('male', 'new')
            release.set(); await engine.wait_idle()
            self.assertIsNone(await job.receive())
            self.assertTrue(finalized.is_set()); self.assertFalse(engine.busy)
            second = engine.start('male', 'new')
            self.assertIsInstance(await second.receive(), bytes)
            second.cancel()
        finally:
            release.set(); await engine.shutdown()

    async def test_backpressure_does_not_exhaust_model_generator(self):
        produced = []; finalized = threading.Event()
        def generate(voice, text):
            try:
                for index in range(100):
                    produced.append(index); yield np.ones(22050, dtype=np.float32)
            finally: finalized.set()
        engine = runtime.Engine(generate, {'male': Path('/voice.wav')})
        job = engine.start('male', 'hello')
        await asyncio.sleep(.15)
        self.assertLess(len(produced), 3)
        job.cancel(); await engine.shutdown()
        self.assertTrue(finalized.is_set())

    async def test_error_is_terminal_and_does_not_leak_model_paths(self):
        def generate(voice, text):
            raise RuntimeError('/private/model/weights secret')
            yield
        engine = runtime.Engine(generate, {'male': Path('/voice.wav')})
        job = engine.start('male', 'hello')
        with self.assertRaisesRegex(RuntimeError, '^Synthesis failed$'):
            await job.receive()
        await engine.shutdown(); self.assertFalse(engine.busy)


class WebSocketTests(unittest.TestCase):
    def test_speaker_selection_pcm_completion_and_health(self):
        calls = []
        def generate(voice, text):
            calls.append((voice, text)); yield np.ones(22050, dtype=np.float32) * 700
        engine = runtime.Engine(generate, {'male': Path('/male.wav'), 'female': Path('/female.wav')})
        with TestClient(create_app(engine)) as client:
            self.assertTrue(client.get('/health').json()['ready'])
            self.assertEqual(client.post('/api/tts/list').json()['speaker_ids'], ['male', 'female'])
            with client.websocket_connect('/ws/tts') as ws:
                for item in messages('female'): ws.send_json(item)
                pcm = b''
                while True:
                    event = ws.receive()
                    if event.get('bytes') is not None: pcm += event['bytes']
                    elif event.get('text'):
                        result = json.loads(event['text']); self.assertTrue(result['is_end']); break
                    else: self.fail(event)
            self.assertEqual(len(pcm), 48000)
            self.assertEqual(calls, [(Path('/female.wav'), '介绍一下语音 agent 技术。')])

    def test_malformed_handshake_never_calls_model(self):
        engine = runtime.Engine(lambda *_: self.fail('model was called'), {'male': Path('/male.wav')})
        with TestClient(create_app(engine)) as client:
            with client.websocket_connect('/ws/tts') as ws:
                for item in messages('unknown'): ws.send_json(item)
                self.assertIn('error', ws.receive_json())
            self.assertFalse(engine.busy)

    def test_binary_handshake_returns_public_protocol_error(self):
        engine = runtime.Engine(lambda *_: self.fail('model was called'), {'male': Path('/male.wav')})
        with TestClient(create_app(engine)) as client:
            with client.websocket_connect('/ws/tts') as ws:
                ws.send_bytes(b'invalid control message')
                self.assertEqual(ws.receive_json()['code'], 400)
            self.assertFalse(engine.busy)

    def test_deadline_cancels_output_without_releasing_active_model(self):
        entered = threading.Event(); release = threading.Event()
        def generate(*_):
            entered.set(); release.wait(3); yield np.ones(22050, dtype=np.float32)
        engine = runtime.Engine(generate, {'male': Path('/male.wav')})
        with TestClient(create_app(engine, generation_timeout=.05)) as client:
            try:
                with client.websocket_connect('/ws/tts') as ws:
                    for item in messages(): ws.send_json(item)
                    self.assertEqual(ws.receive_json()['code'], 504)
                self.assertTrue(entered.is_set())
                self.assertTrue(engine.busy)
            finally: release.set()

    def test_disconnect_requests_cancel_but_remains_busy_until_model_exits(self):
        entered = threading.Event(); release = threading.Event()
        def generate(*_):
            entered.set(); release.wait(3); yield np.ones(22050, dtype=np.float32)
        engine = runtime.Engine(generate, {'male': Path('/male.wav')})
        with TestClient(create_app(engine)) as client:
            try:
                with client.websocket_connect('/ws/tts') as ws:
                    for item in messages(): ws.send_json(item)
                    self.assertTrue(entered.wait(1))
                self.assertTrue(client.get('/health').json()['busy'])
                with client.websocket_connect('/ws/tts') as ws:
                    for item in messages(): ws.send_json(item)
                    self.assertEqual(ws.receive_json()['code'], 409)
            finally: release.set()


if __name__ == '__main__': unittest.main()
