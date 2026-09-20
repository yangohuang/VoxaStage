"""Reproducible avatar-adapter benchmarks; never implies browser or lip-sync quality."""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import wave

import numpy as np
import soxr

from avatar_eval_metrics import MediaMeasurement, summarize
from avatar_providers import ProviderRegistry


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    source: str
    transcript: str
    transcript_reviewed: bool
    input_sha256: str
    pcm_sha256: str
    pcm: bytes

    def public(self):
        return {key: getattr(self, key) for key in (
            'id', 'category', 'source', 'transcript', 'transcript_reviewed',
            'input_sha256', 'pcm_sha256')} | {'audio_duration_s': len(self.pcm) / 48000}


def load_cases(manifest, *, manifest_bytes=None):
    manifest = Path(manifest)
    if manifest_bytes is None:
        with manifest.open("rb") as source:
            manifest_bytes = source.read(2_000_001)
    if len(manifest_bytes) > 2_000_000:
        raise ValueError('Manifest exceeds size limit')
    data = json.loads(manifest_bytes)
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or not isinstance(data.get('cases'), list) or not 1 <= len(data['cases']) <= 100):
        raise ValueError('Expected version 1 and 1–100 cases')
    cases, ids = [], set()
    for row in data['cases']:
        if not isinstance(row, dict):
            raise ValueError('Invalid case')
        case_id = row.get('id')
        if (not isinstance(case_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', case_id)
                or case_id in ids):
            raise ValueError('Case IDs must be unique ASCII identifiers')
        ids.add(case_id)
        if (row.get('category') not in ('zh', 'en', 'mixed', 'silence', 'pause', 'other')
                or row.get('source') not in ('synthetic', 'recorded', 'generated_silence')
                or type(row.get('transcript_reviewed')) is not bool
                or not isinstance(row.get('transcript'), str) or len(row['transcript']) > 4000
                or not isinstance(row.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                or not isinstance(row.get('wav'), str) or not row['wav']):
            raise ValueError('Invalid case provenance or SHA256')
        path = manifest.parent / row['wav']
        if path.stat().st_size > 8_000_000:
            raise ValueError('WAV exceeds size limit')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('WAV SHA256 mismatch')
        # Decode the bytes we hashed, avoiding a second read of a changing file.
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            rate, frames = wav.getframerate(), wav.getnframes()
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or not 8000 <= rate <= 48000 or not 0 < frames <= rate * 30):
                raise ValueError('Expected 0–30 seconds of mono PCM16 at 8–48kHz')
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError('Truncated WAV')
        if rate != 24000:
            pcm = soxr.resample(np.frombuffer(pcm, '<i2'), rate, 24000).astype('<i2').tobytes()
        if not pcm or len(pcm) > 30 * 48000:
            raise ValueError('Invalid normalized PCM duration')
        cases.append(Case(case_id, row['category'], row['source'], row['transcript'],
                          row['transcript_reviewed'], row['sha256'],
                          hashlib.sha256(pcm).hexdigest(), pcm))
    return cases


def provenance():
    root = Path(__file__).resolve().parent
    source_files = ['benchmark_avatar.py', 'avatar_eval_metrics.py', 'avatar_backend.py',
                    'avatar_video_backend.py', 'dinet_backend.py', 'avatar_providers.py']
    versions = {}
    for name in ('numpy', 'soxr', 'websockets', 'pillow', 'av'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
                                         stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {'python': platform.python_version(), 'platform': platform.system(),
            'git_commit': commit, 'packages': versions,
            'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in source_files}}


def render_markdown(report):
    lines = ['# 数字人适配层基线', '', f"状态：{report['status']}。模式：{report['mode']}。", '',
             '测量对象为适配器接收的配对媒体，包含连接、模型、传输、校验及已有节奏控制；不是裸模型推理速度。',
             '不包含 ASR / LLM / TTS 或浏览器播放，不证明口型质量。预热、取消与恢复单独记录。', '',
             '| 驱动 | 样本 | 成功 / 尝试 | 首媒体 P50 / P95（秒） | 完整耗时 P50 / P95（秒） |',
             '| --- | --- | --- | --- | --- |']
    def pair(stats):
        if not stats['n']:
            return '未得到成功样本'
        return f"{stats['p50']:.3f} / {stats['p95']:.3f}"
    for row in report.get('summary', []):
        lines.append(f"| {row['provider']} | {row['case_id']} | {row['passed']} / {row['attempted']} | "
                     f"{pair(row['metrics']['first_media_s'])} | {pair(row['metrics']['wall_s'])} |")
    lines += ['', '分位数采用线性插值；小样本 P95 仅描述本次运行，不能视为稳定性承诺。', '',
              '未评估：浏览器音画实际播放、口型同步质量、身份保持、视觉伪影、物理麦克风回声。',
              'PCM 一致与时间戳连续是协议正确性，不是上述质量指标。', '',
              '详细版本、哈希、失败计数和每轮结果见 results.json。', '']
    return '\n'.join(lines)


def save_report(output, report):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, text in [('results.json', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'),
                       ('report.md', render_markdown(report))]:
        temporary = output / ('.' + name + '.tmp')
        temporary.write_text(text)
        os.replace(temporary, output / name)


async def close_stream(stream):
    # Finish teardown independently of the inference deadline and caller cancel.
    # Real adapters bound their network close operations; this is a final guard.
    async def bounded_close():
        async with asyncio.timeout(10):
            await stream.aclose()
    cleanup = asyncio.create_task(bounded_close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await asyncio.shield(cleanup)
        raise


async def trial(case, args, factory, phase, index):
    row = {'provider': args.provider, 'case_id': case.id, 'input_sha256': case.input_sha256,
           'pcm_sha256': case.pcm_sha256, 'mode': args.mode, 'phase': phase, 'index': index,
           'status': 'failed'}
    meter = MediaMeasurement(case.pcm)
    started = time.monotonic()

    async def source():
        began = time.monotonic()
        for offset in range(0, len(case.pcm), 12000):
            chunk = case.pcm[offset:offset + 12000]
            yield chunk
            if args.mode == 'realtime':
                await asyncio.sleep(max(0., began + (offset + len(chunk)) / 48000 - time.monotonic()))
    try:
        stream = factory().stream(source())
        try:
            async with asyncio.timeout(args.timeout):
                async for event in stream:
                    meter.add(event, time.monotonic() - started)
                    if event['type'] == 'clip_end' or (phase == 'interrupt' and event['type'] == 'media'):
                        break
        finally:
            await close_stream(stream)
        row['metrics'] = meter.finish(time.monotonic() - started, interrupted=phase == 'interrupt')
        row['status'] = 'passed'
    except Exception as exc:
        # URLs, backend error bodies and deployment paths can contain private data.
        row['error_type'] = type(exc).__name__
        row['wall_s'] = time.monotonic() - started
    return row


def validate_args(args):
    if args.provider not in ('flashhead', 'dinet', 'streamingtalker') or args.mode not in ('burst', 'realtime'):
        raise ValueError('Invalid provider or mode')
    if (type(args.rounds) is not int or not 1 <= args.rounds <= 100
            or type(args.warmup) is not int or not 0 <= args.warmup <= 5
            or type(args.timeout) not in (int, float) or not math.isfinite(args.timeout)
            or not 0 < args.timeout <= 300 or type(args.interrupt) is not bool
            or not isinstance(args.deployment_label, str) or not args.deployment_label.strip()
            or len(args.deployment_label) > 200):
        raise ValueError('Invalid benchmark parameters')


async def run_benchmark(args, *, backend_factory=None):
    report = {'version': 1, 'status': 'running', 'passed': False, 'mode': args.mode,
              'created_at': datetime.now(timezone.utc).isoformat(), 'trials': [], 'summary': [],
              'scope': 'avatar_adapter_receive; not model-only RTF, speech pipeline or device playback',
              'quality': {key: {'measured': False} for key in (
                  'lip_sync', 'identity', 'visual_artifacts', 'browser_playback', 'physical_audio')}}
    save_report(args.output, report)  # Invalidate old success even on validation failure.
    try:
        validate_args(args)
        with Path(args.manifest).open("rb") as source:
            manifest_bytes = source.read(2_000_001)
        cases = load_cases(args.manifest, manifest_bytes=manifest_bytes)
        report.update(deployment_label=args.deployment_label, provenance=provenance(),
                      manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                      cases=[case.public() for case in cases],
                      parameters={'rounds': args.rounds, 'warmup': args.warmup,
                                  'timeout_s': args.timeout, 'interrupt': args.interrupt})
        factory = backend_factory or ProviderRegistry(args.config).resolve(args.provider).make_backend
        for case in cases:
            phases = ['warmup'] * args.warmup + ['measured'] * args.rounds
            if args.interrupt:
                phases += ['interrupt', 'recovery']
            for index, phase in enumerate(phases):
                row = await trial(case, args, factory, phase, index)
                report['trials'].append(row)
                report['summary'] = summarize(report['trials'])
                save_report(args.output, report)
                print(json.dumps({key: row[key] for key in ('provider', 'case_id', 'phase', 'status')}), flush=True)
        report['passed'] = all(row['status'] == 'passed' for row in report['trials'])
        report['status'] = 'passed' if report['passed'] else 'failed'
    except BaseException as exc:
        report.update(status='aborted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed',
                      error_type=type(exc).__name__)
        save_report(args.output, report)
        raise
    save_report(args.output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--provider', choices=('flashhead', 'dinet', 'streamingtalker'), required=True)
    parser.add_argument('--config', type=Path, help='Deployment-owned avatar-config.json; never included in report')
    parser.add_argument('--deployment-label', required=True, help='Non-secret model revision / hardware label')
    parser.add_argument('--output', type=Path, default=Path('artifacts/avatar-benchmark'))
    parser.add_argument('--mode', choices=('burst', 'realtime'), default='burst')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--interrupt', action='store_true')
    args = parser.parse_args()
    try:
        report = asyncio.run(run_benchmark(args))
    except Exception as exc:
        parser.exit(1, f'Benchmark failed ({type(exc).__name__}); inspect results.json and deployment configuration.\n')
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
