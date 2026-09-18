"""StreamingTalker adapter; pairs each mesh with the original 24 kHz TTS PCM."""
import asyncio
import base64
import json
import math

import numpy as np
import soxr
import websockets


class AvatarBackend:
    def __init__(self, url, *, connect=websockets.connect, max_audio_seconds=30):
        self.url = url
        self.connect = connect
        self.max_samples = max_audio_seconds * 24000

    async def _open(self):
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            ws = await self.connect(self.url, proxy=None, max_size=2_000_000,
                                    open_timeout=5, close_timeout=.5, max_queue=8)
            try:
                await ws.send(json.dumps({'type': 'start'}))
                meta = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if meta.get('type') == 'metadata':
                    return ws, meta
                if meta.get('code') != 'busy' or asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(f'Avatar unavailable: {meta.get("code", meta.get("type"))}')
            except BaseException:
                await ws.close()
                raise
            await ws.close()
            await asyncio.sleep(.1)

    @staticmethod
    async def _receive(ws, sender):
        receiver = asyncio.create_task(ws.recv())
        try:
            async with asyncio.timeout(20):
                done, _ = await asyncio.wait([receiver, sender], return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    sender.result()
                return json.loads(await receiver)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    async def stream(self, source):
        """Yield metadata, paired media and clip_end; closing cancels model work."""
        ws, meta = await self._open()
        sender = None
        completed = False
        try:
            count, fps = meta.get('vertex_count'), meta.get('fps')
            faces = np.asarray(meta.get('faces'))
            if (type(count) is not int or not 3 <= count <= 20000
                    or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 60
                    or meta.get('sample_rate') != 16000
                    or faces.ndim != 2 or faces.shape[1] != 3 or not 1 <= len(faces) <= 40000
                    or faces.dtype.kind not in 'iu' or faces.min() < 0 or faces.max() >= count):
                raise ValueError('Invalid avatar metadata')
            yield dict(type='avatar_meta', vertex_count=count, fps=fps,
                       faces=faces.tolist(), sample_rate=24000)
            pcm = bytearray()
            resampler = soxr.ResampleStream(24000, 16000, 1, dtype='int16', quality='HQ')

            async def send():
                converted_samples = 0
                async def converted(data, last=False):
                    nonlocal converted_samples
                    block = resampler.resample_chunk(np.frombuffer(data, '<i2'), last=last).astype('<i2').tobytes()
                    if last:
                        # soxr rounds the output length. Rounding up across a
                        # video boundary makes the backend emit an empty extra
                        # mesh. Floor model input duration; preserve every
                        # original TTS sample in the playback track below.
                        remaining = len(pcm) // 2 * 16000 // 24000 - converted_samples
                        block = block[:max(0, remaining) * 2]
                    converted_samples += len(block) // 2
                    for offset in range(0, len(block), 6400):
                        await ws.send(block[offset:offset + 6400])
                async for chunk in source:
                    if len(chunk) % 2 or len(pcm) + len(chunk) > self.max_samples * 2:
                        raise ValueError('Invalid PCM or avatar clip exceeds 30 seconds')
                    pcm.extend(chunk)
                    await converted(chunk)
                await converted(b'', last=True)
                await ws.send(json.dumps({'type': 'end'}))

            sender = asyncio.create_task(send())
            index, sent_samples, last_vertices = 0, 0, None

            def packet(vertices, end):
                nonlocal sent_samples
                event = dict(type='media', frame_index=index, pts=sent_samples / 24000,
                             start_sample=sent_samples, vertices=vertices,
                             audio=base64.b64encode(pcm[sent_samples * 2:end * 2]).decode())
                sent_samples = end
                return event

            while True:
                event = await self._receive(ws, sender)
                kind = event.get('type')
                if kind == 'frame':
                    raw = base64.b64decode(event['vertices'], validate=True)
                    values = np.frombuffer(raw, '<f4')
                    if (len(values) != count * 3 or not np.isfinite(values).all()
                            or event.get('index') != index
                            or not math.isfinite(event.get('pts_seconds', float('nan')))
                            or abs(event['pts_seconds'] - index / fps) > .002):
                        raise ValueError('Invalid avatar vertices or frame sequence')
                    end = min(round((index + 1) * 24000 / fps), len(pcm) // 2)
                    if end <= sent_samples:
                        raise ValueError('Avatar frame has no corresponding audio')
                    last_vertices = event['vertices']
                    yield packet(last_vertices, end)
                    index += 1
                elif kind == 'done':
                    await sender
                    if not event.get('cleanup_complete') or not index:
                        raise RuntimeError('Avatar ended without frames or cleanup confirmation')
                    total = len(pcm) // 2
                    # The model emits whole video frames; preserve a sub-frame
                    # PCM tail by holding the last pose for that final interval.
                    if total > sent_samples:
                        if total - sent_samples > math.ceil(24000 / fps):
                            raise ValueError('Avatar ended before covering the audio')
                        yield packet(last_vertices, total)
                    completed = True
                    yield dict(type='clip_end', total_samples=total)
                    return
                elif kind in ('error', 'cancelled'):
                    raise RuntimeError(f'Avatar stopped: {event.get("code", kind)}')
                elif kind != 'progress':
                    raise ValueError('Unexpected avatar event')
        finally:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if not completed:
                try:
                    await asyncio.wait_for(ws.send(json.dumps({'type': 'cancel'})), .3)
                except Exception:
                    pass
            await ws.close()
