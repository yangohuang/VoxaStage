"""Local, anonymous review of bounded captures. Never invokes avatar models.

Only human-submitted annotations with reviewerKind=human are human reviews;
serving a capture or an automated annotation does not establish visual quality.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import uuid
import wave

from aiohttp import web
import numpy as np
from PIL import Image

MAX_JSON = 2_000_000
MAX_DECODED = 256 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def bounded_read(root, relative, limit):
    """Open beneath a trusted root with no symlinks, including parent components."""
    parts = Path(relative).parts
    if not parts or any(p in ('', '.', '..', '/') for p in parts) or Path(relative).is_absolute():
        raise ValueError('invalid artifact path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('artifact exceeds limit or is not a regular file')
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError('artifact exceeds limit')
            return raw
    finally:
        os.close(descriptor)


def read_json(root, name, limit=MAX_JSON):
    return json.loads(bounded_read(root, name, limit))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def array_bytes(raw, dtype, rows):
    """Validate the NPY header before np.load can allocate from an untrusted shape."""
    stream = io.BytesIO(raw)
    version = np.lib.format.read_magic(stream)
    if version == (1, 0):
        shape, fortran, actual = np.lib.format.read_array_header_1_0(stream, max_header_size=1024)
    elif version == (2, 0):
        shape, fortran, actual = np.lib.format.read_array_header_2_0(stream, max_header_size=1024)
    else:
        raise ValueError('unsupported array version')
    if (len(shape) != 2 or shape[1] != 3 or not rows[0] <= shape[0] <= rows[1]
            or fortran or actual != np.dtype(dtype) or actual.hasobject
            or len(raw) - stream.tell() != shape[0] * 3 * 4):
        raise ValueError('invalid array shape or type')
    values = np.load(io.BytesIO(raw), allow_pickle=False, max_header_size=1024)
    if not np.isfinite(values).all():
        raise ValueError('nonfinite array')
    return values


class Clip:
    def __init__(self, root, relative):
        self.root, self.relative = root, relative
        self.provider, self.case = relative.parts
        self.manifest = self.json('manifest.json')
        self.meta = self.json('metadata.json')
        self.index = self.json('index.json')
        m = self.meta
        if (not isinstance(m, dict) or m.get('sample_rate') != 24000
                or not number(m.get('fps')) or not 1 <= m['fps'] <= 60):
            raise ValueError('invalid metadata')
        self.kind, self.fps = m.get('kind'), m['fps']
        if (not isinstance(self.index, list) or not 1 <= len(self.index) <= 1801
                or self.manifest.get('n_frames') != len(self.index)
                or self.manifest.get('pcm_exact') is not True):
            raise ValueError('invalid frame count or PCM status')
        self.topology_sha = None
        self.faces = None
        if self.kind == '2d':
            if m.get('codec') != 'jpeg' or not all(integer(m.get(k), 1, 2048) for k in ('width', 'height')):
                raise ValueError('invalid JPEG metadata')
            decoded_frame = m['width'] * m['height'] * 4
        elif self.kind == '3d':
            if not integer(m.get('vertex_count'), 3, 20000) or m.get('topology') != 'topology.npy':
                raise ValueError('invalid mesh metadata')
            topology = self.read('topology.npy', 40000*12 + 2048)
            faces = array_bytes(topology, '<i4', (1, 40000))
            if faces.min() < 0 or faces.max() >= m['vertex_count']:
                raise ValueError('invalid topology indices')
            self.faces = faces.tolist()
            self.topology_sha = digest(topology)
            decoded_frame = m['vertex_count'] * 12
        else:
            raise ValueError('unsupported capture kind')
        if decoded_frame * len(self.index) + 30*24000*4 > MAX_DECODED:
            raise ValueError('clip exceeds decoded memory limit')
        wav = self.read('output.wav', 30*24000*2 + 65536)
        self.wav_sha = digest(wav)
        with wave.open(io.BytesIO(wav), 'rb') as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 24000, 'NONE'):
                raise ValueError('invalid WAV format')
            samples = stream.getnframes()
            if not 1 <= samples <= 30*24000:
                raise ValueError('invalid WAV duration')
            pcm = stream.readframes(samples)
            if len(pcm) != samples*2:
                raise ValueError('truncated WAV')
        pcm_sha = digest(pcm)
        if pcm_sha != self.manifest.get('output_pcm_sha256'):
            raise ValueError('PCM hash mismatch')
        self.duration = samples / 24000
        start = 0
        suffix = 'jpg' if self.kind == '2d' else 'npy'
        for i, frame in enumerate(self.index):
            expected_start = round(i * 24000 / self.fps)
            expected_end = min(samples, round((i+1) * 24000 / self.fps))
            if (not isinstance(frame, dict) or type(frame.get('frame_index')) is not int
                    or frame['frame_index'] != i or type(frame.get('start_sample')) is not int
                    or frame['start_sample'] != start or abs(start - expected_start) > 1
                    or not integer(frame.get('n_samples'), 1, 24000)
                    or abs(frame['n_samples'] - (expected_end-start)) > 1
                    or not number(frame.get('pts')) or abs(frame['pts'] - start/24000) > 1e-6
                    or abs(frame['pts'] - i/self.fps) > 1/24000 + 1e-6
                    or frame.get('artifact') != f'frames/{i:06d}.{suffix}'
                    or not isinstance(frame.get('payload_sha256'), str)
                    or not SHA.fullmatch(frame['payload_sha256'])):
                raise ValueError('invalid frame index or sample clock')
            start += frame['n_samples']
            self.frame(i)
        if start != samples:
            raise ValueError('frames do not cover WAV')
        self.fingerprint = digest(canonical(dict(metadata=m, index=self.index, pcm=pcm_sha, topology=self.topology_sha)))
        self.category = next((c for c in ('zh', 'en', 'mixed', 'silence', 'pause')
                              if self.case == c or self.case.startswith(c+'_') or self.case.startswith(c+'-')), 'other')

    def read(self, name, limit):
        return bounded_read(self.root, self.relative / name, limit)

    def json(self, name):
        return json.loads(self.read(name, MAX_JSON))

    def topology(self):
        if self.kind == '3d' and digest(self.read('topology.npy', 40000*12+2048)) != self.topology_sha:
            raise ValueError('capture changed')

    def frame(self, index):
        frame = self.index[index]
        if self.kind == '2d':
            raw = self.read(f'frames/{index:06d}.jpg', 1_400_000)
            if digest(raw) != frame['payload_sha256']:
                raise ValueError('frame hash mismatch')
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != 'JPEG' or image.size != (self.meta['width'], self.meta['height']):
                    raise ValueError('invalid JPEG dimensions')
                image.load()
        else:
            self.topology()
            count = self.meta['vertex_count']
            raw = array_bytes(self.read(f'frames/{index:06d}.npy', count*12+2048), '<f4', (count, count)).astype('<f4', copy=False).tobytes()
            if digest(raw) != frame['payload_sha256']:
                raise ValueError('frame hash mismatch')
        return raw

    def public(self, detail=False):
        value = dict(id=self.id, kind=self.kind, category=self.category, duration=self.duration,
                     fps=self.fps, frames=len(self.index), fingerprint=self.fingerprint)
        if detail:
            self.topology()
            value.update(sampleRate=24000, audioUrl=f'/api/clips/{self.id}/audio',
                         frames=[dict(index=i, pts=f['pts'], url=f'/api/clips/{self.id}/frames/{i}')
                                 for i, f in enumerate(self.index)])
            if self.kind == '2d':
                value.update(width=self.meta['width'], height=self.meta['height'])
            else:
                value.update(vertexCount=self.meta['vertex_count'], faces=self.faces)
        return value


def load_captures(captures):
    root = Path(captures).absolute()
    clips = []
    scanned = 0
    # Bound discovery too, so a directory with millions of failures is rejected.
    with os.scandir(root) as providers:
        for provider in providers:
            if not provider.is_dir(follow_symlinks=False):
                continue
            with os.scandir(provider.path) as cases:
                for case in cases:
                    scanned += 1
                    if scanned > 10000:
                        raise ValueError('capture scan exceeds limit')
                    if not case.is_dir(follow_symlinks=False):
                        continue
                    relative = Path(provider.name) / case.name
                    manifest = read_json(root, relative / 'manifest.json')
                    if not isinstance(manifest, dict):
                        raise ValueError('invalid manifest')
                    if manifest.get('status') != 'passed':
                        continue
                    if len(clips) >= 100:
                        raise ValueError('more than 100 successful clips')
                    try:
                        clips.append(Clip(root, relative))
                    except (OSError, ValueError, KeyError, TypeError, EOFError, wave.Error) as exc:
                        raise ValueError('invalid successful capture') from exc
    return sorted(clips, key=lambda c: (c.provider, c.case))


def write_exclusive(path, value):
    raw = canonical(value) + b'\n'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def validate_annotation(value, clips):
    if not isinstance(value, dict):
        raise ValueError('annotation must be an object')
    fields = {'clipId', 'fingerprint', 'reviewer', 'reviewerKind', 'dimension', 'start', 'end', 'severity', 'notes', 'reviewed'}
    if set(value) != fields:
        raise ValueError('invalid annotation fields')
    clip = clips.get(value['clipId']) if isinstance(value['clipId'], str) else None
    if clip is None or value['fingerprint'] != clip.fingerprint:
        raise ValueError('unknown clip or fingerprint mismatch')
    if (value['reviewed'] is not True or value['reviewerKind'] not in ('human', 'automated')
            or not isinstance(value['reviewer'], str) or not 1 <= len(value['reviewer'].strip()) <= 128
            or len(value['reviewer']) > 128
            or not isinstance(value['notes'], str) or len(value['notes']) > 4096
            or value['dimension'] not in ('timing', 'phonetics', 'continuity', 'appearance', 'pose')
            or (clip.kind == '3d' and value['dimension'] == 'appearance')
            or not integer(value['severity'], 0, 3)
            or not number(value['start']) or not number(value['end'])
            or not 0 <= value['start'] < value['end'] <= clip.duration):
        raise ValueError('invalid annotation values')
    return dict(value)


def make_app(captures, output, port=18426, seed=20260919):
    if not integer(port, 1, 65535):
        raise ValueError('invalid port')
    ordered = load_captures(captures)
    random.Random(seed).shuffle(ordered)
    for index, clip in enumerate(ordered, 1):
        clip.id = f'clip-{index:03d}'
    clips = {clip.id: clip for clip in ordered}
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError('review output must not be a symlink')
    key = {c.id: dict(provider=c.provider, case=c.case, fingerprint=c.fingerprint) for c in ordered}
    try:
        write_exclusive(output / 'key.json', key)
    except FileExistsError:
        if read_json(output, 'key.json') != key:
            raise ValueError('review key differs; use a new output directory')
    annotations = []
    for path in sorted(output.glob('annotation-*.json')):
        if len(annotations) >= 10000:
            raise ValueError('too many annotations')
        record = read_json(output, path.name, 32768)
        if not isinstance(record, dict) or not isinstance(record.get('id'), str) or not isinstance(record.get('createdAt'), str):
            raise ValueError('invalid persisted annotation')
        validate_annotation({k: v for k, v in record.items() if k not in ('id', 'createdAt')}, clips)
        annotations.append(record)
    authority = f'127.0.0.1:{port}'

    @web.middleware
    async def boundary(request, handler):
        if request.headers.getall('Host', []) != [authority]:
            raise web.HTTPForbidden(text='invalid host')
        if request.method == 'POST' and request.headers.getall('Origin', []) != ['http://' + authority]:
            raise web.HTTPForbidden(text='invalid origin')
        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, OSError, KeyError, TypeError, EOFError, wave.Error):
            raise web.HTTPConflict(text='capture unavailable or changed') from None
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    def selected(request):
        clip = clips.get(request.match_info['id'])
        if clip is None:
            raise web.HTTPNotFound(text='unknown clip')
        return clip

    async def listing(request):
        return web.json_response(dict(clips=[c.public() for c in ordered]))

    async def detail(request):
        return web.json_response(selected(request).public(detail=True))

    async def audio(request):
        clip = selected(request)
        clip.topology()
        raw = clip.read('output.wav', 30*24000*2+65536)
        if digest(raw) != clip.wav_sha:
            raise ValueError('WAV changed')
        return web.Response(body=raw, content_type='audio/wav')

    async def frame(request):
        clip = selected(request)
        value = request.match_info['frame']
        if not re.fullmatch(r'0|[1-9][0-9]{0,3}', value) or int(value) >= len(clip.index):
            raise web.HTTPNotFound(text='unknown frame')
        raw = clip.frame(int(value))
        return web.Response(body=raw, content_type='image/jpeg' if clip.kind == '2d' else 'application/octet-stream')

    async def get_annotations(request):
        return web.json_response(dict(annotations=annotations))

    async def annotate(request):
        if request.content_type != 'application/json':
            raise web.HTTPBadRequest(text='JSON required')
        try:
            value = validate_annotation(await request.json(), clips)
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text='invalid annotation') from None
        if len(annotations) >= 10000:
            raise web.HTTPBadRequest(text='annotation limit reached')
        value.update(id=str(uuid.uuid4()), createdAt=datetime.now(timezone.utc).isoformat())
        write_exclusive(output / f"annotation-{value['id']}.json", value)
        annotations.append(value)
        return web.json_response(value, status=201)

    static = {'/': ('evaluation/review.html', 'text/html'),
              '/review.mjs': ('evaluation/review.mjs', 'text/javascript'),
              '/review-core.mjs': ('evaluation/review-core.mjs', 'text/javascript'),
              '/renderer.mjs': ('avatar-web/renderer.mjs', 'text/javascript')}

    async def page(request):
        name, mime = static[request.path]
        return web.Response(body=bounded_read(Path(__file__).parent, name, MAX_JSON), content_type=mime)

    app = web.Application(middlewares=[boundary], client_max_size=16384)
    app.router.add_get('/api/clips', listing)
    app.router.add_get('/api/clips/{id}', detail)
    app.router.add_get('/api/clips/{id}/audio', audio)
    app.router.add_get('/api/clips/{id}/frames/{frame}', frame)
    app.router.add_get('/api/annotations', get_annotations)
    app.router.add_post('/api/annotations', annotate)
    for route in static:
        app.router.add_get(route, page)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18426)
    parser.add_argument('--seed', type=int, default=20260919)
    args = parser.parse_args()
    try:
        app = make_app(args.captures, args.output, args.port, args.seed)
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(2, 'Review startup rejected invalid captures, annotations, or output mapping.\n')
    web.run_app(app, host='127.0.0.1', port=args.port, access_log=None)


if __name__ == '__main__':
    main()
