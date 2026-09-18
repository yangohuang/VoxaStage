"""Version-1 2D bridge adapter, preserving original TTS PCM and cancellation.

FlashHead implements this wire protocol; DINet uses a native connection adapter.
See AVATAR-PROTOCOL.md for service implementation requirements.
"""
import asyncio
import base64
import io
import json
import math

from PIL import Image
import websockets

from avatar_backend import AvatarBackend


class VideoBackend:
    def __init__(self, url, *, connect=websockets.connect, max_audio_seconds=30):
        self.url, self.connect = url, connect
        self.max_samples = max_audio_seconds * 24000

    @staticmethod
    def metadata(meta):
        if (not isinstance(meta, dict) or meta.get('type') != 'metadata' or meta.get('protocol') != 1
                or meta.get('sample_rate') != 24000 or meta.get('kind') != '2d' or meta.get('codec') != 'jpeg'
                or type(meta.get('fps')) not in (int, float) or not 1 <= meta['fps'] <= 60
                or any(type(meta.get(k)) is not int or not 1 <= meta[k] <= 2048 for k in ('width', 'height'))):
            raise ValueError('Invalid 2D avatar metadata')
        return {key: meta[key] for key in ('kind', 'codec', 'sample_rate', 'fps', 'width', 'height')}

    @staticmethod
    def validate_image(encoded, meta):
        if not isinstance(encoded, str) or len(encoded) > 1_400_000:
            raise ValueError('Invalid avatar image size')
        try:
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != 'JPEG' or image.size != (meta['width'], meta['height']):
                    raise ValueError('Invalid avatar image dimensions or format')
                image.verify()
        except Exception as exc:
            raise ValueError('Invalid avatar image') from exc

    async def _open(self):
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            ws = await self.connect(self.url, proxy=None, max_size=1_500_000, max_queue=4,
                                    open_timeout=5, close_timeout=.5)
            try:
                await ws.send(json.dumps(dict(type='start', protocol=1, sample_rate=24000)))
                first = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if isinstance(first, dict) and first.get('type') == 'error':
                    if first.get('code') != 'busy' or asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError(f'2D avatar unavailable: {first.get("code", "backend_error")}')
                else:
                    return ws, self.metadata(first)
            except BaseException:
                await ws.close()
                raise
            await ws.close()
            await asyncio.sleep(.1)

    async def stream(self, source):
        ws, meta = await self._open()
        sender, completed = None, False
        try:
            yield dict(type='avatar_meta', **meta)
            pcm = bytearray()

            async def send():
                async for chunk in source:
                    if not isinstance(chunk, bytes) or len(chunk) % 2 or len(pcm) + len(chunk) > self.max_samples * 2:
                        raise ValueError('Invalid PCM or avatar clip exceeds 30 seconds')
                    pcm.extend(chunk)
                    for offset in range(0, len(chunk), 12000):
                        await ws.send(chunk[offset:offset + 12000])
                await ws.send(json.dumps({'type': 'end'}))

            sender = asyncio.create_task(send())
            playback_start = None
            index, sent_samples, last_image = 0, 0, None
            fps = meta['fps']

            def packet(image, end):
                nonlocal sent_samples
                event = dict(type='media', frame_index=index, pts=sent_samples / 24000,
                             start_sample=sent_samples, image=image,
                             audio=base64.b64encode(pcm[sent_samples * 2:end * 2]).decode())
                sent_samples = end
                return event

            while True:
                event = await AvatarBackend._receive(ws, sender)
                if not isinstance(event, dict):
                    raise ValueError('Invalid 2D avatar event')
                kind = event.get('type')
                if kind == 'frame':
                    pts = event.get('pts_seconds')
                    if (type(event.get('index')) is not int or event['index'] != index
                            or type(pts) not in (int, float) or not math.isfinite(pts)
                            or abs(pts - index / fps) > .002):
                        raise ValueError('Invalid avatar frame sequence')
                    self.validate_image(event.get('image'), meta)
                    end = min(round((index + 1) * 24000 / fps), len(pcm) // 2)
                    if end <= sent_samples:
                        raise ValueError('Avatar frame has no corresponding audio')
                    # Fast offline inference must not fill the browser with
                    # hundreds of decoded images before playback can catch up.
                    now = asyncio.get_running_loop().time()
                    if playback_start is None:
                        playback_start = now
                    delay = playback_start + sent_samples / 24000 - 1.0 - now
                    if delay > 0:
                        await asyncio.sleep(delay)
                    last_image = event['image']
                    yield packet(last_image, end)
                    index += 1
                elif kind == 'done':
                    await sender
                    if event.get('cleanup_complete') is not True or not index:
                        raise RuntimeError('Avatar ended without frames or cleanup confirmation')
                    total = len(pcm) // 2
                    if total > sent_samples:
                        if total - sent_samples > math.ceil(24000 / fps):
                            raise ValueError('Avatar ended before covering the audio')
                        yield packet(last_image, total)
                    completed = True
                    yield dict(type='clip_end', total_samples=total)
                    return
                elif kind in ('error', 'cancelled'):
                    raise RuntimeError(f'Avatar stopped: {event.get("code", kind)}')
                elif kind != 'progress':
                    raise ValueError('Unexpected 2D avatar event')
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
