"""Bounded still-image attachments with explicit client capture provenance."""
import base64
import io
import math
import re

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 262144


def decode_images(images, seen_ids):
    if not isinstance(images, list) or not 1 <= len(images) <= 2:
        raise ValueError('Expected one or two image attachments')
    content = []
    for item in images:
        if not isinstance(item, dict) or set(item) != {'id', 'source', 'captured_at_ms', 'data'}:
            raise ValueError('Invalid image attachment fields')
        identifier, captured = item['id'], item['captured_at_ms']
        if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier)
                or identifier in seen_ids or len(seen_ids) >= 2
                or item['source'] not in ('upload', 'camera')
                or type(captured) not in (int, float) or not 0 <= captured <= 600000
                or not math.isfinite(captured)):
            raise ValueError('Invalid or duplicate image provenance; at most two images per request')
        data = item['data']
        if not isinstance(data, str) or not 1 <= len(data) <= 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError('Image exceeds 256KiB')
        try:
            raw = base64.b64decode(data, validate=True)
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError('Image exceeds 256KiB')
            with Image.open(io.BytesIO(raw)) as image:
                if (image.format not in ('PNG', 'JPEG') or getattr(image, 'n_frames', 1) != 1
                        or not 1 <= image.width <= 1024 or not 1 <= image.height <= 1024):
                    raise ValueError('Expected static JPEG/PNG no larger than 1024x1024')
                image.load()
                decoded = image.convert('RGB')
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ValueError('Invalid image data') from exc
        seen_ids.add(identifier)
        content.extend([
            f'[画面 {identifier}，来源 {item["source"]}，客户端会话相对采集时间 {captured}ms；不是物理同步时间。]',
            decoded,
        ])
    return content


def prefill_chunks(content):
    """Attach images/text once, while retaining native one-second audio chunks."""
    import numpy as np
    prefix = [part for part in content if not isinstance(part, np.ndarray)]
    audios = [part for part in content if isinstance(part, np.ndarray)]
    if not audios:
        return [prefix]
    if len(audios) != 1:
        raise ValueError('Expected one audio input per message')
    audio = audios[0]
    return [(prefix if offset == 0 else []) + [audio[offset:offset + 16000]]
            for offset in range(0, len(audio), 16000)]
