import asyncio
import base64
import json
import unittest

import numpy as np

from avatar_backend import AvatarBackend


class FakeSocket:
    def __init__(self, bad_frame=False):
        self.sent = []
        self.closed = False
        self.ended = asyncio.Event()
        self.bad_frame = bad_frame
        self.index = -1

    async def send(self, message):
        self.sent.append(message)
        if isinstance(message, str) and json.loads(message)['type'] == 'end':
            self.ended.set()

    async def recv(self):
        if self.index == -1:
            self.index = 0
            return json.dumps(dict(type='metadata', sample_rate=16000, fps=30,
                                   vertex_count=3, faces=[[0, 1, 2]]))
        await self.ended.wait()
        if self.index == 3:
            return json.dumps(dict(type='done', cleanup_complete=True))
        vertices = np.zeros((3, 3), dtype='<f4')
        if self.bad_frame:
            vertices[0, 0] = np.nan
        message = dict(type='frame', index=self.index, pts_seconds=self.index / 30,
                       vertices=base64.b64encode(vertices.tobytes()).decode())
        self.index += 1
        return json.dumps(message)

    async def close(self):
        self.closed = True


class AvatarBackendTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, pcm, socket):
        async def connect(*args, **kwargs):
            return socket
        async def source():
            for i in range(0, len(pcm), 960):
                yield pcm[i:i + 960]
        return [event async for event in AvatarBackend('ws://test', connect=connect).stream(source())]

    async def test_pcm_preserved_including_partial_final_video_frame(self):
        pcm = np.arange(2500, dtype='<i2').tobytes()
        socket = FakeSocket()
        events = await self.collect(pcm, socket)
        media = [e for e in events if e['type'] == 'media']
        self.assertEqual(b''.join(base64.b64decode(e['audio']) for e in media), pcm)
        self.assertEqual([e['start_sample'] for e in media], [0, 800, 1600, 2400])
        self.assertEqual(events[-1]['total_samples'], 2500)
        self.assertTrue(socket.closed)
        self.assertTrue(all(len(p) <= 6400 for p in socket.sent if isinstance(p, bytes)))

    async def test_non_finite_mesh_is_rejected_and_connection_closed(self):
        socket = FakeSocket(bad_frame=True)
        with self.assertRaisesRegex(ValueError, 'vertices'):
            await self.collect(bytes(4800), socket)
        self.assertTrue(socket.closed)

    async def test_resampling_rounding_does_not_create_an_extra_empty_mesh_frame(self):
        class ClockSocket(FakeSocket):
            async def recv(self):
                if self.index == -1:
                    return await super().recv()
                await self.ended.wait()
                samples = sum(len(p) for p in self.sent if isinstance(p, bytes)) // 2
                frames = (samples * 30 + 15999) // 16000
                if self.index == frames:
                    return json.dumps(dict(type='done', cleanup_complete=True))
                i = self.index
                self.index += 1
                return json.dumps(dict(type='frame', index=i, pts_seconds=i / 30,
                                       vertices=base64.b64encode(bytes(36)).decode()))
        socket = ClockSocket()
        pcm = bytes(25600 * 2)  # exactly 32 video frames, but 17066.666 model samples
        events = await self.collect(pcm, socket)
        media = [e for e in events if e['type'] == 'media']
        self.assertEqual(len(media), 32)
        self.assertEqual(b''.join(base64.b64decode(e['audio']) for e in media), pcm)
        self.assertEqual(events[-1]['total_samples'], 25600)

    async def test_audio_source_failure_wakes_pending_receive(self):
        socket = FakeSocket()
        async def connect(*args, **kwargs):
            return socket
        async def source():
            raise RuntimeError('TTS source failed')
            yield b''
        async def run():
            return [e async for e in AvatarBackend('ws://test', connect=connect).stream(source())]
        with self.assertRaisesRegex(RuntimeError, 'TTS source failed'):
            await asyncio.wait_for(run(), 1)
        self.assertTrue(socket.closed)


if __name__ == '__main__':
    unittest.main()
