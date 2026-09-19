"""Session-scoped pending still image, bound once to a subsequent user turn."""
import base64
import hashlib
import io
import math
import time

from PIL import Image


class VisualContext:
    def __init__(self, session_id, *, enabled=False, clock=time.monotonic):
        self.session_id, self.enabled, self.clock = session_id, enabled, clock
        self.started = clock()
        self.sequence = self.turn = 0
        self.pending = None

    def control(self, message):
        sequence = message.get('sequence')
        if (not self.enabled or message.get('session_id') != self.session_id
                or type(sequence) is not int or not self.sequence < sequence <= 10000
                or message.get('operation') not in ('set', 'clear')):
            raise ValueError('画面所属会话或序号无效，请重新提交。')
        pending = None
        if message['operation'] == 'set':
            image = message.get('image')
            if not isinstance(image, dict) or set(image) != {'id', 'source', 'captured_at_ms', 'data'}:
                raise ValueError('画面信息不完整。')
            stamp, data = image['captured_at_ms'], image['data']
            if (image['id'] != f'image-{sequence}' or image['source'] not in ('upload', 'camera')
                    or type(stamp) not in (int, float) or not 0 <= stamp <= 600000 or not math.isfinite(stamp)
                    or not isinstance(data, str) or not 1 <= len(data) <= 349528):
                raise ValueError('画面元数据或大小超限。')
            try:
                raw = base64.b64decode(data, validate=True)
                if not raw or len(raw) > 262144:
                    raise ValueError('画面超过256KiB。')
                with Image.open(io.BytesIO(raw)) as decoded:
                    if (decoded.format not in ('JPEG', 'PNG') or getattr(decoded, 'n_frames', 1) != 1
                            or not 1 <= decoded.width <= 1024 or not 1 <= decoded.height <= 1024):
                        raise ValueError('请选择不超过1024×1024的静态图片。')
                    decoded.load()
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError('无法解码画面。') from exc
            pending = dict(image=image.copy(), sha256=hashlib.sha256(raw).hexdigest(),
                           received_at_ms=(self.clock() - self.started) * 1000)
        self.sequence, self.pending = sequence, pending
        return dict(type='visual_ack', sequence=sequence, state='pending' if pending else 'cleared',
                    image_id=pending['image']['id'] if pending else None)

    def bind(self, user):
        self.turn += 1
        user = dict(user)
        event = dict(type='visual_bound', turn_id=self.turn, sequence=self.sequence, images=[])
        if self.pending is not None:
            item, self.pending = self.pending, None
            user['images'] = [item['image'].copy()]
            event['images'] = [dict(id=item['image']['id'], source=item['image']['source'],
                                    captured_at_ms=item['image']['captured_at_ms'],
                                    received_at_ms=item['received_at_ms'], sha256=item['sha256'])]
        return user, event

    def clear(self):
        self.pending = None
