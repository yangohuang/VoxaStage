"""Run the existing WAV collector with host admission and immediate trial evidence."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'evaluation'))
from asr_capture import request_json, run

MATCH_FIELDS = ('phase', 'repeat', 'case_id', 'variant', 'audio_sha256', 'context_sha256')


async def collect(args, manifest, *, emit, admit, transport=None):
    expected = manifest['requests']
    index = 0
    async def observe(event, trial):
        nonlocal index
        if index >= len(expected):
            raise RuntimeError('unexpected_extra_trial')
        row = expected[index]
        if any(trial.get(key) != row[key] for key in MATCH_FIELDS):
            raise RuntimeError('frozen_trial_mismatch')
        if event == 'request_ready':
            emit({'event': event, 'case': row['case'], 'input': row})
            await admit(row['case'])
        else:
            emit({'event': 'trial_result', 'case': row['case'], 'trial': trial})
            if trial['status'] != 'ok':
                raise RuntimeError('failed_asr_trial')
            emit({'event': 'complete', 'case': row['case'], 'trial': trial})
            index += 1
    try:
        async with asyncio.timeout(manifest['timeouts_s']['startup']):
            async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
                while True:
                    try:
                        state = await request_json(client, 'GET', args.url+'/health', timeout=2)
                        if (state.get('ready') is True and state.get('busy') is False
                                and state.get('model') == manifest['model_name']):
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.1)
        emit({'event': 'ready', 'health': state})
        report = await run(args, transport=transport, observer=observe,
                           checkpoint=lambda value: emit({'event': 'checkpoint', 'report': value}))
        emit({'event': 'capture', 'report': report})
        if report['status'] != 'passed' or index != len(expected):
            raise RuntimeError('incomplete_asr_capture')
        emit({'event': 'finished', 'requests': index})
        return report
    except Exception as error:
        emit({'event': 'failed', 'error_type': type(error).__name__, 'error': str(error)})
        report = locals().get('report')
        if report is not None and report['status'] != 'passed':
            return report
        return {'status': 'failed', 'error_type': type(error).__name__, 'capture': report}


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    spec = importlib.util.spec_from_file_location('llm_admission', ROOT/'llm-server/deploy/protocol_client.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    async def admit(case):
        await module.admit_stdin(case)
    args = SimpleNamespace(manifest=ROOT/'inputs/cases.json', variants=ROOT/'inputs/variants.json',
                           output=Path('/tmp/capture'), url='http://127.0.0.1:18315',
                           expected_model=manifest['model_name'], model_revision=manifest['model_revision'],
                           rounds=3, timeout=manifest['timeouts_s']['request'])
    def emit(event): print(json.dumps(event, ensure_ascii=False), flush=True)
    report = asyncio.run(collect(args, manifest, emit=emit, admit=admit))
    if report['status'] != 'passed': raise SystemExit(1)

if __name__ == '__main__': main()
