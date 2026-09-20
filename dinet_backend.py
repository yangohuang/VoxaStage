"""Adapt the existing DINet audio WebSocket service to VideoBackend's PCM clock.

Native wire: HEADER 103; IMAGE 101 YUV420P; AUDIO 102; CTL 100.
Only original TTS PCM is played. Native padding and returned audio are discarded.
"""
import asyncio
import base64
import io
import json
import math
import time
from urllib.parse import unquote, urlsplit
import uuid

import av
import numpy as np
import soxr
from PIL import Image
import websockets

from avatar_video_backend import VideoBackend


def encode_image(raw, width, height, output_size):
    planar = np.frombuffer(raw, np.uint8).reshape(height * 3 // 2, width)
    rgb = av.VideoFrame.from_ndarray(planar, format='yuv420p').to_ndarray(format='rgb24')
    image = Image.fromarray(rgb)
    if image.size != output_size:
        image = image.resize(output_size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=85)
    return base64.b64encode(out.getvalue()).decode('ascii')


class NativeConnection:
    def __init__(self, ws, character, *, observer=None):
        self.ws, self.character = ws, character
        self.observer = observer
        self.transaction = 'pipecat-' + uuid.uuid4().hex
        self.control = dict(transaction_id=self.transaction, session_id=self.transaction + '-turn',
                            is_start=0, is_end=0, is_interrupt=0)
        self.width = self.height = 0
        self.output_size = (0, 0)
        self.samples = self.index = self.native_frames = 0
        self.last_native = None
        self.ended = self.closed = False
        self.started = None
        self.resampler = soxr.ResampleStream(24000, 16000, 1, dtype='int16')
        self.input_buffer = bytearray()

    def observe(self, kind, **data):
        if self.observer is not None:
            self.observer(dict(type=kind, **data))

    async def _send_resampled(self, converted, *, last=False):
        self.input_buffer.extend(converted.astype('<i2').tobytes())
        # Legacy native receive code retains rest_chunk after an exactly
        # completed block. Every message is an aligned 200ms block.
        if last and self.samples:
            if len(self.input_buffer) % 6400:
                self.input_buffer.extend(bytes(6400 - len(self.input_buffer) % 6400))
            # Flush the model's trailing feature window; never play this pad.
            self.input_buffer.extend(bytes(6400))
        while len(self.input_buffer) >= 6400:
            block = bytes(self.input_buffer[:6400])
            del self.input_buffer[:6400]
            await self.ws.send(block)
            self.observe('native_audio_sent', bytes=len(block))

    async def send(self, data):
        if isinstance(data, bytes):
            if self.started is None:
                self.started = time.monotonic()
            # The native server consumes 200ms chunks and has a finite queue.
            # Never enqueue an entire fast-generated reply ahead of playback.
            delay = self.started + self.samples / 24000 - 1 - time.monotonic()
            if delay > 0:
                self.observe('input_wait', delay_s=delay)
                await asyncio.sleep(delay)
            self.samples += len(data) // 2
            converted = self.resampler.resample_chunk(np.frombuffer(data, '<i2'))
            await self._send_resampled(converted)
            return
        kind = json.loads(data)['type']
        if kind == 'start':
            await self.ws.send(json.dumps(dict(self.control, open_stream=1,
                character_id=self.character, audio_sr=16000, media_type='frame',
                open_h264=0, input_data_type='audio')))
        elif kind == 'end':
            self.ended = True
            tail = self.resampler.resample_chunk(np.empty(0, dtype=np.int16), last=True)
            await self._send_resampled(tail, last=True)
            await self.ws.send(json.dumps(dict(self.control, is_end=1)))
        elif kind == 'cancel':
            await self.ws.send(json.dumps(dict(self.control, is_end=1, is_interrupt=1)))

    async def _body(self, size):
        body = bytearray()
        while len(body) < size:
            data = await self.ws.recv()
            if not isinstance(data, bytes) or not data or len(body) + len(data) > size:
                raise ValueError(f'Invalid or truncated DINet media payload: received {len(body)}/{size}, next {type(data).__name__}:{len(data)}')
            body.extend(data)
        return bytes(body)

    async def recv(self):
        # Bound ignored audio/control/padding messages per receive operation.
        for _ in range(32):
            raw = await self.ws.recv()
            if not isinstance(raw, str) or len(raw) > 16384:
                raise ValueError('Invalid DINet control message')
            event = json.loads(raw)
            if not isinstance(event, dict) or event.get('code', 0) not in (0, 200):
                raise ValueError('DINet returned an invalid or error response')
            kind = event.get('message_type')
            if kind == 103:
                if self.width or event.get('fps') != 25 or event.get('color') != 'YUV420P':
                    raise ValueError('Unsupported DINet video metadata')
                width, height = event.get('width'), event.get('height')
                if any(type(n) is not int or not 2 <= n <= 2048 or n % 2 for n in (width, height)):
                    raise ValueError('Invalid DINet video dimensions')
                self.width, self.height = width, height
                scale = min(1, 640 / max(width, height))
                self.output_size = (round(width * scale), round(height * scale))
                await self.ws.send(json.dumps(dict(self.control, is_start=1)))
                return json.dumps(dict(type='metadata', protocol=1, kind='2d', sample_rate=24000,
                    fps=25, width=self.output_size[0], height=self.output_size[1], codec='jpeg'))
            if not self.width:
                raise ValueError('DINet media before metadata')
            if kind in (101, 102):
                size = event.get('frame_size')
                expected = self.width * self.height * 3 // 2
                if (type(size) is not int or size <= 0 or
                    (kind == 101 and size != expected) or (kind == 102 and (size > 48000 or size % 2))):
                    raise ValueError('Invalid DINet media size')
                if kind == 101:
                    self.observe('native_frame_header', frame=self.index)
                body = await self._body(size)
                if kind == 101:
                    self.observe('native_frame_body', frame=self.index, bytes=len(body))
                if kind == 102:
                    continue
                number = event.get('frame_no')
                if (type(number) is not int or not 0 <= number < 2**31 or
                    (self.last_native is not None and number != self.last_native + 1)):
                    raise ValueError('Invalid DINet frame sequence')
                self.last_native = number
                self.native_frames += 1
                if (self.native_frames > 760 or
                    (self.ended and self.native_frames > math.ceil(self.samples / 960) + 10)):
                    raise ValueError('DINet output exceeds clip limit')
                if self.ended and self.index >= math.ceil(self.samples / 960):
                    continue  # Native engine pads the last 200ms block.
                self.observe('encode_start', frame=self.index)
                image = await asyncio.to_thread(encode_image, body, self.width, self.height, self.output_size)
                self.observe('encode_end', frame=self.index)
                result = dict(type='frame', index=self.index, pts_seconds=self.index / 25, image=image)
                self.index += 1
                return json.dumps(result)
            if kind == 100:
                if event.get('session_id') not in (None, '', self.control['session_id']):
                    raise ValueError('DINet returned another session')
                if event.get('is_interrupt'):
                    raise RuntimeError('DINet interrupted the clip')
                if event.get('is_end'):
                    if not self.ended:
                        raise ValueError('DINet ended before input completion')
                    # Release this adapter's transport before reporting local
                    # cleanup. Native GPU lease state isn't exposed by this API.
                    await self.close()
                    return json.dumps(dict(type='done', cleanup_complete=True))
                continue
            raise ValueError('Unexpected DINet message type')
        raise ValueError('Excessive DINet control or padding messages')

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.ws.close()


class DINetBackend(VideoBackend):
    def __init__(self, url, *, connect=websockets.connect, observer=None):
        self.native_connect = connect
        self.observer = observer
        super().__init__(url, connect=self._connect)

    async def _open(self):
        # A just-cancelled single worker may still be releasing its runtime.
        # Retry only this explicit pre-stream capacity error, never model errors.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while True:
            try:
                return await super()._open()
            except websockets.exceptions.ConnectionClosed as exc:
                received = exc.rcvd
                if (received is None or received.code != 1011
                        or received.reason != 'Server error: No available workers'
                        or loop.time() >= deadline):
                    raise
                await asyncio.sleep(.1)

    async def _connect(self, url, **kwargs):
        kwargs.update(max_size=8_000_000, max_queue=4)
        ws = await self.native_connect(url, **kwargs)
        return NativeConnection(ws, unquote(urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]), observer=self.observer)
