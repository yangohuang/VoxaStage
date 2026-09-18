import asyncio
import base64
import io
import json
import unittest

from PIL import Image
from avatar_video_backend import VideoBackend


def jpeg():
    out = io.BytesIO()
    Image.new('RGB', (16, 16), (30, 90, 120)).save(out, 'JPEG')
    return base64.b64encode(out.getvalue()).decode()


class Socket:
    def __init__(self, frames=2, image=None):
        self.messages = []
        self.ended = asyncio.Event()
        self.index = -1
        self.frames = frames
        self.image = image or jpeg()
        self.closed = False

    async def send(self, data):
        self.messages.append(data)
        if isinstance(data, str) and json.loads(data)['type'] == 'end':
            self.ended.set()

    async def recv(self):
        if self.index == -1:
            self.index = 0
            return json.dumps(dict(type='metadata', protocol=1, kind='2d', sample_rate=24000,
                                   fps=25, width=16, height=16, codec='jpeg'))
        await self.ended.wait()
        if self.index == self.frames:
            return json.dumps(dict(type='done', cleanup_complete=True))
        index = self.index
        self.index += 1
        return json.dumps(dict(type='frame', index=index, pts_seconds=index / 25, image=self.image))

    async def close(self):
        self.closed = True


class VideoTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, socket, pcm):
        async def connect(*args, **kwargs):
            return socket
        async def source():
            for offset in range(0, len(pcm), 500):
                yield pcm[offset:offset + 500]
        return [event async for event in VideoBackend('ws://test', connect=connect).stream(source())]

    async def test_pcm_and_partial_tail_preserved(self):
        pcm = bytes(range(256)) * 16  # 2048 samples: two full frames + tail
        socket = Socket()
        events = await self.collect(socket, pcm)
        media = [e for e in events if e['type'] == 'media']
        self.assertEqual([e['start_sample'] for e in media], [0, 960, 1920])
        self.assertEqual(b''.join(base64.b64decode(e['audio']) for e in media), pcm)
        self.assertEqual(events[-1]['total_samples'], 2048)
        self.assertEqual(events[0]['kind'], '2d')
        self.assertTrue(socket.closed)
        self.assertEqual(b''.join(x for x in socket.messages if isinstance(x, bytes)), pcm)

    async def test_malformed_image_and_incomplete_video_rejected(self):
        for socket, error in [(Socket(image=base64.b64encode(b'not jpeg').decode()), 'image'),
                              (Socket(frames=1), 'covering')]:
            with self.assertRaisesRegex(ValueError, error):
                await self.collect(socket, bytes(6000))
            self.assertTrue(socket.closed)

    async def test_source_failure_wakes_receiver_and_cancels(self):
        socket = Socket()
        async def connect(*args, **kwargs):
            return socket
        async def source():
            raise RuntimeError('source failed')
            yield b''
        async def run():
            return [e async for e in VideoBackend('ws://test', connect=connect).stream(source())]
        with self.assertRaisesRegex(RuntimeError, 'source failed'):
            await asyncio.wait_for(run(), 1)
        self.assertTrue(socket.closed)
        self.assertIn({'type': 'cancel'}, [json.loads(x) for x in socket.messages if isinstance(x, str)])
