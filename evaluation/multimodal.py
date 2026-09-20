"""Serial four-condition visual/voice evaluation; semantics remain unassessed."""
import argparse
import asyncio
import base64
from contextlib import aclosing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
import wave

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_backend import OmniBackend

MODES = ('audio_only', 'image_only', 'image_text', 'image_audio')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_bounded(path, size):
    with path.open('rb') as source:
        data = source.read(size + 1)
    if len(data) > size:
        raise ValueError('Input exceeds bound')
    return data


def load_cases(manifest, *, manifest_bytes=None):
    manifest = Path(manifest)
    raw = read_bounded(manifest, 200000) if manifest_bytes is None else manifest_bytes
    if len(raw) > 200000:
        raise ValueError('Manifest exceeds bound')
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('cases'), list) or not 1 <= len(value['cases']) <= 20):
        raise ValueError('Expected version1 manifest with 1–20 cases')
    cases, seen = [], set()
    for item in value['cases']:
        if not isinstance(item, dict):
            raise ValueError('Invalid case')
        identifier = item.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or identifier in seen:
            raise ValueError('Invalid or duplicate case ID')
        seen.add(identifier)
        if (not isinstance(item.get('question_text'), str) or not 1 <= len(item['question_text']) <= 1000
                or type(item.get('transcript_reviewed')) is not bool
                or item.get('source') not in ('synthetic', 'human')
                or not isinstance(item.get('expected_facts'), list) or not 1 <= len(item['expected_facts']) <= 10
                or any(not isinstance(f, str) or not 1 <= len(f) <= 200 for f in item['expected_facts'])):
            raise ValueError('Invalid question, provenance or reference facts')
        payload = {}
        for name, limit in (('image', 262144), ('audio', 1000000)):
            relative = item.get(name)
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError('Expected relative input file')
            path = (manifest.parent / relative).resolve()
            if not path.is_relative_to(manifest.parent.resolve()):
                raise ValueError('Input must remain inside manifest directory')
            data = read_bounded(path, limit)
            if digest(data) != item.get(name + '_sha256'):
                raise ValueError('Input hash mismatch')
            payload[name] = data
        with Image.open(io.BytesIO(payload['image'])) as image:
            if (image.format not in ('PNG', 'JPEG') or getattr(image, 'n_frames', 1) != 1
                    or not 1 <= image.width <= 1024 or not 1 <= image.height <= 1024):
                raise ValueError('Expected static JPEG/PNG no larger than1024x1024')
            image.load()
        with wave.open(io.BytesIO(payload['audio']), 'rb') as wav:
            samples = wav.getnframes()
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, 'NONE') or not 0 < samples <= 480000:
                raise ValueError('Expected at most30s PCM16 mono WAV at16kHz')
            pcm = wav.readframes(samples)
            if len(pcm) != samples * 2:
                raise ValueError('Truncated input WAV')
        public = {key: item[key] for key in ('id', 'question_text', 'source', 'transcript_reviewed', 'expected_facts', 'image_sha256', 'audio_sha256')}
        public.update(pcm_sha256=digest(pcm), input_samples=samples)
        cases.append(dict(public=public, image=base64.b64encode(payload['image']).decode(), audio=base64.b64encode(pcm).decode()))
    return cases


def build_messages(case, mode):
    if mode not in MODES:
        raise ValueError('Unknown input condition')
    message = dict(role='user')
    if mode in ('audio_only', 'image_audio'):
        message['audio'] = case['audio']
    else:
        message['text'] = '请简短描述画面。' if mode == 'image_only' else case['public']['question_text']
    if mode != 'audio_only':
        # Neutral ID and synthetic clock: neither carries the reference answer.
        message['images'] = [dict(id='image-1', source='upload', captured_at_ms=0, data=case['image'])]
    return [message]


def persist(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


async def run(args, *, backend=None):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(version=1, status='running', created_at=datetime.now(timezone.utc).isoformat(),
                  execution='injected_backend' if backend is not None else 'http_worker',
                  scope='Worker text/PCM arrival; no browser, microphone or perceptual measurement. No automatic semantic score.',
                  trials=[], summary={}, source_sha256={name: digest((ROOT / name).read_bytes()) for name in ('evaluation/multimodal.py', 'omni_backend.py')})
    path = output / 'results.json'
    persist(path, report)
    client = None
    try:
        if (type(args.rounds) is not int or not 1 <= args.rounds <= 5
                or type(args.timeout) not in (int, float) or not 0 < args.timeout <= 180
                or args.voice not in ('male', 'female')):
            raise ValueError('Invalid run parameters')
        manifest_bytes = read_bounded(Path(args.manifest), 200000)
        cases = load_cases(args.manifest, manifest_bytes=manifest_bytes)
        report.update(cases=[c['public'] for c in cases], manifest_sha256=digest(manifest_bytes),
                      rounds=args.rounds, voice=args.voice, order='rotate modes by round and case index; fixed deterministic schedule')
        if backend is None:
            client = httpx.AsyncClient(trust_env=False, timeout=5)
            response = await client.get(args.url.rstrip('/') + '/health')
            response.raise_for_status()
            health = response.json()
            if health.get('ready') is not True or health.get('capabilities', {}).get('image_input') is not True:
                raise ValueError('Worker does not report ready image capability')
            model = health.get('model')
            report['worker_reported_model'] = model[:200] if isinstance(model, str) else None
            backend = OmniBackend(client, args.url, voice=args.voice)
        schedule = [('warmup', -1, cases[0], 'image_text')]
        for round_index in range(args.rounds):
            for case_index, case in enumerate(cases):
                shift = (round_index + case_index) % len(MODES)
                schedule.extend(('measured', round_index, case, mode) for mode in MODES[shift:] + MODES[:shift])
        for phase, round_index, case, mode in schedule:
            row = dict(index=len(report['trials']), phase=phase, round=round_index, case_id=case['public']['id'],
                       mode=mode, status='running', semantic_assessment=None, text='', output_samples=0)
            report['trials'].append(row)
            persist(path, report)
            began = time.monotonic()
            texts, pcm = [], bytearray()
            messages = build_messages(case, mode)
            row['request_content_sha256'] = digest(json.dumps(messages, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
            try:
                async with asyncio.timeout(args.timeout), aclosing(backend.generate(messages)) as stream:
                    async for kind, value in stream:
                        elapsed = time.monotonic() - began
                        if kind == 'text':
                            if not isinstance(value, str) or sum(map(len, texts)) + len(value) > 4000:
                                raise ValueError('Invalid or excessive output text')
                            row.setdefault('first_text_s', elapsed)
                            texts.append(value)
                        elif kind == 'audio':
                            if not isinstance(value, bytes) or not value or len(value) % 2 or len(pcm) + len(value) > 1440000:
                                raise ValueError('Invalid or excessive PCM output')
                            row.setdefault('first_audio_s', elapsed)
                            pcm.extend(value)
                        else:
                            raise ValueError('Unexpected model output kind')
                if not pcm:
                    raise ValueError('Missing generated audio')
                row['status'] = 'passed'
            except BaseException as exc:
                row.update(status='failed', error_type=type(exc).__name__)
                if not isinstance(exc, Exception):
                    raise
            finally:
                row.update(text=''.join(texts), output_samples=len(pcm) // 2, pcm_sha256=digest(pcm), wall_s=time.monotonic() - began)
                if args.save_audio and pcm:
                    with wave.open(str(output / f'{row["index"]:03d}.wav'), 'wb') as wav:
                        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                        wav.writeframes(pcm)
                persist(path, report)
        for mode in MODES:
            rows = [row for row in report['trials'] if row['phase'] == 'measured' and row['mode'] == mode]
            passed = sum(row['status'] == 'passed' for row in rows)
            report['summary'][mode] = dict(attempted=len(rows), passed=passed, failed=len(rows) - passed,
                                           semantic_assessed=0, semantic_passed=None)
        report['status'] = 'passed' if all(row['status'] == 'passed' for row in report['trials']) else 'failed'
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        persist(path, report)
        raise
    finally:
        if client is not None:
            await client.aclose()
        persist(path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', required=True, help='Explicit local worker or authorized tunnel URL; omitted from reports')
    parser.add_argument('--voice', choices=('male', 'female'), default='female')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--save-audio', action='store_true', help='Keep generated WAV locally for listening review')
    try:
        result = asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        parser.exit(1, f'Evaluation failed ({type(exc).__name__}); inspect results.json.\n')
    print(json.dumps(dict(status=result['status'], trials=len(result['trials']), summary=result['summary'])))
    raise SystemExit(result['status'] != 'passed')


if __name__ == '__main__':
    main()
