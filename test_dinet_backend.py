import asyncio
import base64
import io
import json
import math
import unittest

import av
import numpy as np
import soxr
from PIL import Image
from dinet_backend import DINetBackend, NativeConnection


class NativeSocket:
    def __init__(self, *, bad_size=False, gap=False, truncated=False, frames=5):
        self.sent = []
        self.closed = False
        self.ended = asyncio.Event()
        rgb = np.full((16, 16, 3), [180, 50, 30], dtype=np.uint8)
        self.raw = av.VideoFrame.from_ndarray(rgb, format='rgb24').to_ndarray(format='yuv420p').tobytes()
        self.events = [json.dumps(dict(message_type=103, code=0, width=16, height=16, fps=25, color='YUV420P'))]
        self.bad_size, self.gap, self.truncated = bad_size, gap, truncated
        self.prepared = False
        self.frames = frames

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get('is_end'):
            self.ended.set()

    async def recv(self):
        if self.events:
            return self.events.pop(0)
        await self.ended.wait()
        if not self.prepared:
            self.prepared = True
            for i in range(self.frames):
                self.events += [json.dumps(dict(message_type=101, frame_no=i + (1 if self.gap and i == 1 else 0),
                                                frame_size=len(self.raw) + (1 if self.bad_size else 0))),
                                self.raw[:100], self.raw[100:]]
                self.events += [json.dumps(dict(message_type=102, frame_no=i, frame_size=1920)), bytes(1920)]
            if self.truncated:
                self.events[2] = json.dumps(dict(message_type=100, is_end=1))
            self.events += [json.dumps(dict(message_type=100, is_end=1))]
            return self.events.pop(0)
        raise RuntimeError('read beyond end')

    async def close(self):
        self.closed = True


class DINetTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_input_marked_before_padding_can_produce_frames(self):
        socket = NativeSocket()
        bridge = NativeConnection(socket, 'test')
        original_send = socket.send
        states = []
        async def send(data):
            if isinstance(data, bytes):
                states.append(bridge.ended)
            await original_send(data)
        socket.send = send
        await bridge.send(bytes(2002))
        await bridge.send(json.dumps({'type': 'end'}))
        self.assertTrue(states and all(states))

    async def test_native_input_has_only_complete_200ms_blocks_before_final_tail(self):
        socket = NativeSocket()
        bridge = NativeConnection(socket, 'test')
        await bridge.send(json.dumps({'type': 'start'}))
        await bridge.recv()
        for _ in range(13):
            await bridge.send(bytes(3000))
        await bridge.send(json.dumps({'type': 'end'}))
        blocks = [x for x in socket.sent if isinstance(x, bytes)]
        self.assertTrue(all(len(x) == 6400 for x in blocks[:-1]))
        self.assertTrue(0 < len(blocks[-1]) <= 6400)
        # 13000 resampled samples, rounded to 200ms plus one feature-flush block.
        self.assertEqual(sum(map(len, blocks)), 38400)

    def backend(self, socket):
        async def connect(*args, **kwargs):
            return socket
        return DINetBackend('ws://localhost/api/ws/live_video/test', connect=connect)

    async def collect(self, socket):
        self.pcm = bytes(range(256)) * 7 + bytes(210)  # 1001 samples
        async def source():
            yield self.pcm[:500]
            yield self.pcm[500:]
        return [e async for e in self.backend(socket).stream(source())]

    async def test_native_fragmented_video_tail_and_original_audio(self):
        socket = NativeSocket()
        events = await self.collect(socket)
        media = [e for e in events if e['type'] == 'media']
        self.assertEqual(len(media), 2)
        self.assertEqual(b''.join(base64.b64decode(e['audio']) for e in media), self.pcm)
        self.assertEqual(events[-1]['total_samples'], 1001)
        image = Image.open(io.BytesIO(base64.b64decode(media[0]['image'])))
        self.assertEqual(image.size, (16, 16))
        self.assertTrue(all(abs(a-b) < 10 for a,b in zip(image.getpixel((0, 0)), (180, 50, 30))))
        config = json.loads(socket.sent[0])
        self.assertEqual((config['input_data_type'], config['audio_sr'], config['open_h264']), ('audio', 16000, 0))
        expected = soxr.resample(np.frombuffer(self.pcm, '<i2'), 24000, 16000)
        converted = np.frombuffer(b''.join(x for x in socket.sent if isinstance(x, bytes)), '<i2')
        # Integer-output soxr applies dither; tolerate two quantization steps.
        np.testing.assert_allclose(converted[:len(expected)].astype(float), expected.astype(float), atol=2, rtol=0)
        self.assertEqual(len(converted), math.ceil(len(expected) / 3200) * 3200 + 3200)
        self.assertFalse(converted[len(expected):].any())
        self.assertTrue(socket.closed)

    async def test_bad_size_gap_and_truncated_frame_fail_closed(self):
        for options in ({'bad_size':True}, {'gap':True}, {'truncated':True}):
            socket = NativeSocket(**options)
            with self.assertRaises(ValueError):
                await self.collect(socket)
            self.assertTrue(socket.closed)

    async def test_excess_native_frames_reject_sample_rate_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'clip limit'):
            await self.collect(NativeSocket(frames=52))

    async def test_cancel_closes_and_sends_interrupt(self):
        socket = NativeSocket()
        async def source():
            yield bytes(2002)
        stream = self.backend(socket).stream(source())
        await anext(stream)
        await anext(stream)
        await stream.aclose()
        self.assertTrue(socket.closed)
        self.assertTrue(any(isinstance(x, str) and json.loads(x).get('is_interrupt') == 1 for x in socket.sent))

    async def test_source_error_does_not_wait_for_native_media(self):
        socket = NativeSocket()
        async def source():
            raise RuntimeError('source failed')
            yield b''
        async def run():
            return [e async for e in self.backend(socket).stream(source())]
        with self.assertRaisesRegex(RuntimeError, 'source failed'):
            await asyncio.wait_for(run(), 1)
        self.assertTrue(socket.closed)
