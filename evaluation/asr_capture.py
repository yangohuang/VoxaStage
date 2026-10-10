"""Collect bounded local ASR requests with frozen input bytes and explicit provenance."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import wave

import httpx

try:
    from .asr_scoring import score
except ImportError:  # Direct CLI entry.
    from asr_scoring import score

MAX_JSON = 200_000
MAX_AUDIO = 1_000_000
MAX_REPLY = 65_536
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bounded(path, limit):
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file exceeds size limit')
    return data


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_inputs(path, raw):
    path = Path(path)
    value = json.loads(raw)
    if (len(raw) > MAX_JSON or not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or not isinstance(value.get('cases'), list)
            or not 1 <= len(value['cases']) <= 20):
        raise ValueError('expected version 1 manifest with 1..20 cases')
    cases, seen, sources = [], set(), set()
    for item in value['cases']:
        if not isinstance(item, dict):
            raise ValueError('invalid case')
        identifier = item.get('id')
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in seen:
            raise ValueError('invalid or duplicate case ID')
        seen.add(identifier)
        relative = item.get('audio')
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError('audio must be a relative file')
        audio_path = (path.parent / relative).resolve()
        if not audio_path.is_relative_to(path.parent.resolve()):
            raise ValueError('audio must be within manifest directory')
        audio = bounded(audio_path, MAX_AUDIO)
        if digest(audio) != item.get('sha256'):
            raise ValueError('audio hash mismatch')
        try:
            with wave.open(io.BytesIO(audio), 'rb') as wav:
                samples = wav.getnframes()
                if ((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype())
                        != (16000, 1, 2, 'NONE') or not 0 < samples <= 480000):
                    raise ValueError('expected 0..30s mono PCM16 WAV at 16kHz')
                pcm = wav.readframes(samples)
                if len(pcm) != samples * 2:
                    raise ValueError('truncated WAV')
        except (wave.Error, EOFError) as exc:
            raise ValueError('invalid WAV') from exc
        if item.get('source') not in ('synthetic', 'human'):
            raise ValueError('source must identify synthetic or human audio')
        sources.add(item['source'])
        if len(sources) > 1:
            raise ValueError('use separate manifests for synthetic and human audio')
        if item['source'] == 'human' and item.get('reference_kind') != 'human_transcript':
            raise ValueError('human audio requires a reviewed transcript')
        public = {key: item.get(key) for key in ('reference', 'reference_kind', 'transcript_reviewed', 'source')}
        # Reuse the scoring provenance contract, including truly empty references.
        score([dict(public, case_id=identifier, variant='validation', status='ok', text='')])
        public.update(id=identifier, audio_sha256=digest(audio), pcm_sha256=digest(pcm),
                      input_samples=samples, audio_s=samples / 16000)
        cases.append(dict(public=public, audio=audio))
    return cases


def load_variants(raw):
    value = json.loads(raw)
    if (len(raw) > MAX_JSON or not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or not isinstance(value.get('variants'), list)
            or not 1 <= len(value['variants']) <= 8):
        raise ValueError('expected version 1 variants with 1..8 conditions')
    result, seen = [], set()
    for item in value['variants']:
        if not isinstance(item, dict):
            raise ValueError('invalid variant')
        name, context = item.get('id'), item.get('context')
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in seen:
            raise ValueError('invalid or duplicate variant ID')
        if (not isinstance(context, str) or len(context) > 500 or context != context.strip()
                or any(ord(c) < 32 or ord(c) > 126 for c in context)):
            raise ValueError('context must be at most 500 printable ASCII characters without edge whitespace')
        seen.add(name)
        result.append(dict(id=name, context=context, context_sha256=digest(context.encode('ascii'))))
    return result


def persist(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


async def request_json(client, method, url, *, timeout, **kwargs):
    async with asyncio.timeout(timeout):
        async with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_REPLY:
                    raise ValueError('response exceeds size limit')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError('expected object response')
            return value


def validate_args(args):
    url = urlsplit(args.url)
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or url.username is not None or url.password is not None or url.query or url.fragment
            or url.path not in ('', '/') or url.port == 0):
        raise ValueError('use a local HTTP endpoint, optionally through an existing tunnel')
    if (type(args.rounds) is not int or not 1 <= args.rounds <= 5
            or not finite(args.timeout) or not 0 < args.timeout <= 180
            or not isinstance(args.expected_model, str) or not 1 <= len(args.expected_model) <= 200
            or (args.model_revision is not None and
                (not isinstance(args.model_revision, str) or not 1 <= len(args.model_revision) <= 200))):
        raise ValueError('invalid run settings')


async def run(args, *, transport=None):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    path = output / 'results.json'
    report = dict(version=1, status='running', created_at=datetime.now(timezone.utc).isoformat(),
                  execution='injected_transport' if transport is not None else 'http_worker',
                  scope='Completed WAV request/response timing; not streaming input or physical microphone latency.',
                  trials=[], source_sha256={name: digest(Path(__file__).with_name(name).read_bytes())
                    for name in ('asr_capture.py', 'asr_scoring.py')})
    persist(path, report)
    try:
        validate_args(args)
        manifest = bounded(args.manifest, MAX_JSON)
        variant_bytes = bounded(args.variants, MAX_JSON)
        cases, variants = load_inputs(args.manifest, manifest), load_variants(variant_bytes)
        report.update(manifest_sha256=digest(manifest), variants_file_sha256=digest(variant_bytes),
                      cases=[c['public'] for c in cases], variants=variants,
                      expected_model=args.expected_model, declared_model_revision=args.model_revision,
                      reported_model_revision=None, rounds=args.rounds, timeout_s=args.timeout,
                      order='one warmup per variant, then rotate variants by round and case index')
        persist(path, report)
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=args.timeout) as client:
            endpoint = args.url.rstrip('/')
            report['endpoint'] = endpoint
            health = await request_json(client, 'GET', endpoint + '/health', timeout=args.timeout)
            if health.get('ready') is not True or health.get('busy') is True or health.get('model') != args.expected_model:
                raise ValueError('ASR must be ready, idle and report the expected model')
            report['health_snapshot_sha256'] = digest(json.dumps(health, sort_keys=True, allow_nan=False).encode())
            report['reported_execution_metadata'] = {
                key: health[key] if isinstance(health.get(key), str) and len(health[key]) <= 200 else None
                for key in ('device', 'dtype')}
            report['reported_execution_metadata']['threads'] = (
                health['threads'] if type(health.get('threads')) is int and 0 < health['threads'] <= 1024 else None)
            # Health reports are not a complete weight/tokenizer/decoder fingerprint.
            report['model_configuration_complete'] = False
            revision = health.get('revision')
            if isinstance(revision, str) and 0 < len(revision) <= 200:
                report['reported_model_revision'] = revision
            if args.model_revision is not None and revision is not None and revision != args.model_revision:
                raise ValueError('declared and reported model revisions disagree')
            schedule = [('warmup', -1, cases[0], variant) for variant in variants]
            for repeat in range(args.rounds):
                for index, case in enumerate(cases):
                    shift = (repeat + index) % len(variants)
                    schedule.extend(('measured', repeat, case, variant)
                                    for variant in variants[shift:] + variants[:shift])
            scoring = []
            report['planned_trials'] = len(schedule)
            report['planned_measured_trials'] = len(cases) * len(variants) * args.rounds
            for phase, repeat, case, variant in schedule:
                public = case['public']
                trial = dict(phase=phase, repeat=repeat, case_id=public['id'], variant=variant['id'],
                             audio_sha256=public['audio_sha256'], context_sha256=variant['context_sha256'],
                             status='running', text=None)
                report['trials'].append(trial)
                persist(path, report)
                started = time.perf_counter()
                try:
                    response = await request_json(client, 'POST', endpoint + '/transcribe', timeout=args.timeout,
                        content=case['audio'], headers={'content-type': 'audio/wav', 'x-asr-context': variant['context']})
                    if (response.get('model') != args.expected_model
                            or not isinstance(response.get('text'), str) or len(response['text']) > 4000
                            or not finite(response.get('audio_s')) or abs(response['audio_s'] - public['audio_s']) > 1/16000
                            or not finite(response.get('elapsed_s')) or response['elapsed_s'] < 0):
                        raise ValueError('response model, text, duration or timing is invalid')
                    trial.update(status='ok', text=response['text'], model=response['model'],
                                 server_elapsed_s=response['elapsed_s'])
                except Exception as exc:
                    trial.update(status='error', error_type=type(exc).__name__)
                    if isinstance(exc, httpx.HTTPStatusError):
                        trial['http_status'] = exc.response.status_code
                except BaseException:
                    trial['status'] = 'aborted'
                    raise
                finally:
                    trial['request_wall_s'] = time.perf_counter() - started
                    persist(path, report)
                if phase == 'measured':
                    scoring.append(dict(case_id=f'{public["id"]}@r{repeat}', variant=variant['id'],
                        reference=public['reference'], reference_kind=public['reference_kind'],
                        transcript_reviewed=public['transcript_reviewed'], status=trial['status'], text=trial['text']))
            report['scoring'] = score(scoring)
            persist(output / 'scoring-input.json', scoring)
            report['status'] = 'passed' if all(t['status'] == 'ok' for t in report['trials']) else 'failed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
    except BaseException:
        report['status'] = 'aborted'
        raise
    finally:
        persist(path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--variants', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:18315')
    parser.add_argument('--expected-model', required=True)
    parser.add_argument('--model-revision')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except (OSError, ValueError) as exc:
        parser.error(type(exc).__name__)
    print(json.dumps(dict(status=report['status'], trials=len(report['trials']))))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
