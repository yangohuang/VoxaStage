"""Real loopback HTTP/WebSocket checks; waveform model is deliberately synthetic."""
import asyncio
import json
import socket
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
import uvicorn
import websockets

from backend import LocalBackend
from tts_runtime import Engine
from tts_server import create_app
from test_tts_server import messages


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, generate):
        self.engine = Engine(generate, {'pipecat_male': Path('/male.wav'), 'female11': Path('/female.wav')})
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0)); self.socket.listen(); self.socket.setblocking(False)
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.engine), log_level='error', lifespan='on'))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.task.done(): await self.task; self.fail('server exited before ready')
                await asyncio.sleep(.01)
        self.url = f'http://127.0.0.1:{self.port}'
        self.ws_url = f'ws://127.0.0.1:{self.port}/ws/tts'

    async def asyncTearDown(self):
        if hasattr(self, 'release'): self.release.set()
        if hasattr(self, 'server'):
            self.server.should_exit = True
            await asyncio.wait_for(self.task, 5)
            self.socket.close()

    async def test_existing_backend_receives_full_correct_rate_audio_for_both_voices(self):
        calls = []
        def generate(voice, text):
            calls.append((voice.name, text))
            # Two segments including a short final tail exercise persistent resampling.
            yield np.full(16000, 1200, dtype=np.float32)
            yield np.full(6051, 1200, dtype=np.float32)
        await self.start(generate)
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            with patch('backend.TTS_URL', self.url):
                for voice in ('male', 'female'):
                    async with asyncio.timeout(5):
                        pcm = b''.join([p async for p in LocalBackend(client, voice=voice).synthesize('语音 agent')])
                    self.assertEqual(len(pcm) // 2, round(22051 * 24000 / 22050))
            self.assertFalse((await client.get(self.url + '/health')).json()['busy'])
        self.assertEqual(calls, [('male.wav', '语音 agent'), ('female.wav', '语音 agent')])

    async def test_transport_close_while_model_blocked_then_recovery(self):
        entered = threading.Event(); self.release = threading.Event()
        def generate(*_):
            entered.set(); self.release.wait(5)
            yield np.full(22050, 900, dtype=np.float32)
        await self.start(generate)
        async with websockets.connect(self.ws_url, proxy=None) as ws:
            for item in messages('pipecat_male'): await ws.send(json.dumps(item))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        async with websockets.connect(self.ws_url, proxy=None) as ws:
            for item in messages('pipecat_male'): await ws.send(json.dumps(item))
            self.assertEqual(json.loads(await ws.recv())['code'], 409)
        self.release.set()
        await asyncio.wait_for(self.engine.wait_idle(), 2)
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            with patch('backend.TTS_URL', self.url):
                async with asyncio.timeout(5):
                    pcm = b''.join([p async for p in LocalBackend(client, voice='male').synthesize('恢复对话')])
                self.assertEqual(len(pcm), 48000)


if __name__ == '__main__': unittest.main()
