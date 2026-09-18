"""Capture adapter outputs for offline inspection, separately from timing benchmarks."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from avatar_eval_capture import ClipCapture
from avatar_providers import ProviderRegistry
from benchmark_avatar import close_stream, load_cases, provenance


async def run(args, *, backend_factory=None):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(version=1, status='running', provider=args.provider, clips=[],
                  created_at=datetime.now(timezone.utc).isoformat(),
                  perceptual_quality_measured=False,
                  scope='offline output capture; disk IO and decoding included; not a performance benchmark')

    def save():
        temporary = output / '.results.json.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        temporary.replace(output / 'results.json')

    save()
    try:
        if (args.provider not in ('flashhead', 'dinet', 'streamingtalker')
                or type(args.timeout) not in (int, float) or not math.isfinite(args.timeout)
                or not 0 < args.timeout <= 300):
            raise ValueError('Invalid capture parameters')
        with Path(args.manifest).open('rb') as source:
            manifest_bytes = source.read(2_000_001)
        cases = load_cases(args.manifest, manifest_bytes=manifest_bytes)
        deployment_label = getattr(args, 'deployment_label', 'test backend' if backend_factory else '')
        if not isinstance(deployment_label, str) or not deployment_label.strip() or len(deployment_label) > 500:
            raise ValueError('A non-secret deployment descriptor is required')
        report.update(deployment_label=deployment_label, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                      provenance=provenance(), timeout_s=args.timeout)
        root = Path(__file__).resolve().parent
        for name in ('capture_avatar.py', 'avatar_eval_capture.py'):
            report['provenance']['source_sha256'][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        factory = backend_factory or ProviderRegistry(args.config).resolve(args.provider).make_backend
        for case in cases:
            row = dict(case=case.public(), status='running')
            report['clips'].append(row)
            save()
            capture = None
            try:
                capture = ClipCapture(output / case.id, case.pcm)
                async def source():
                    for offset in range(0, len(case.pcm), 12000):
                        yield case.pcm[offset:offset + 12000]
                started = time.monotonic()
                stream = factory().stream(source())
                try:
                    async with asyncio.timeout(args.timeout):
                        async for event in stream:
                            capture.add(event, time.monotonic() - started)
                finally:
                    await close_stream(stream)
                row.update(status='passed', capture=capture.finish(time.monotonic() - started))
            except BaseException as exc:
                row.update(status='failed', error_type=type(exc).__name__)
                if capture is not None:
                    capture.fail(type(exc).__name__)
                if not isinstance(exc, Exception):
                    raise
            save()
            print(json.dumps(dict(provider=args.provider, case_id=case.id, status=row['status'])), flush=True)
        report['status'] = 'passed' if all(row['status'] == 'passed' for row in report['clips']) else 'failed'
    except BaseException as exc:
        report.update(status='aborted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed',
                      error_type=type(exc).__name__)
        save()
        raise
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--provider', choices=('flashhead', 'dinet', 'streamingtalker'), required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--deployment-label', required=True, help='Non-secret model revision, character alias, settings, device and residency')
    parser.add_argument('--output', type=Path, required=True, help='New output directory')
    parser.add_argument('--timeout', type=float, default=90)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        parser.exit(1, f'Capture failed ({type(exc).__name__}); inspect results.json.\n')
    raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
