"""Prepare local, explicitly synthetic probes for avatar evaluation."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import wave

import httpx

from backend import LocalBackend

PROMPTS = {
    'zh': ['你好，欢迎来到语音实验室。', '请帮我介绍一下实时数字人。', '我们先听完这句话，再开始回答。'],
    'en': ['Hello, welcome to the voice laboratory.', 'Please explain how a talking avatar works.',
           'Wait until I finish speaking before you answer.'],
    'mixed': ['介绍一下语音 agent 技术。', '请解释一下 VAD 和 ASR 的区别。',
              '我们用 WebRTC 传输语音，用 Python 处理模型输出。'],
}


async def prepare(output, synthesize, *, voice_label):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    provenance = {'version': 1, 'status': 'running', 'voice_label': voice_label,
                  'created_at': datetime.now(timezone.utc).isoformat(),
                  'synthesized_cases': 0, 'prompts': PROMPTS,
                  'scope': 'controlled synthetic probes, not human speech or verified transcripts',
                  'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'pause_composition': []}
    def save():
        temporary = output / '.provenance.tmp'
        temporary.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(output / 'provenance.json')
    save()
    cases, spoken = [], {}
    def add(case_id, category, pcm, transcript, source='synthetic'):
        if not pcm or len(pcm) % 2 or len(pcm) > 30 * 48000:
            raise ValueError('Expected 0–30 seconds of complete PCM16')
        path = output / (case_id + '.wav')
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(pcm)
        cases.append(dict(id=case_id, category=category, wav=path.name,
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          source=source, transcript=transcript, transcript_reviewed=False))
    try:
        for category, prompts in PROMPTS.items():
            for index, text in enumerate(prompts):
                pcm = bytearray()
                async with asyncio.timeout(90):
                    async for chunk in synthesize(text):
                        if not isinstance(chunk, bytes) or len(pcm) + len(chunk) > 30 * 48000:
                            raise ValueError('Invalid or excessive synthesis output')
                        pcm.extend(chunk)
                data = bytes(pcm)
                case_id = f'{category}-{index:02d}'
                add(case_id, category, data, text)
                spoken[case_id] = data
                provenance['synthesized_cases'] += 1
                save()
        for index, seconds in enumerate((1, 2, 3)):
            add(f'silence-{index:02d}', 'silence', bytes(seconds * 48000), '', 'generated_silence')
        for index, gap_ms in enumerate((400, 700, 1000)):
            left, right = f'zh-{index:02d}', f'zh-{(index+1)%3:02d}'
            gap = bytes(gap_ms * 48)
            pcm = spoken[left] + gap + spoken[right]
            case_id = f'pause-{index:02d}'
            add(case_id, 'pause', pcm, PROMPTS['zh'][index] + PROMPTS['zh'][(index+1)%3])
            provenance['pause_composition'].append(dict(id=case_id, left=left, right=right,
                 inserted_silence_start_sample=len(spoken[left])//2, inserted_silence_samples=len(gap)//2))
        manifest = {'version': 1, 'cases': cases}
        temporary = output / '.cases.tmp'
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(output / 'cases.json')
        provenance.update(status='passed', cases=len(cases),
                          manifest_sha256=hashlib.sha256((output/'cases.json').read_bytes()).hexdigest())
        save()
        return manifest
    except BaseException as exc:
        provenance.update(status='failed', error_type=type(exc).__name__)
        save()
        raise


async def run(args):
    async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
        backend = LocalBackend(client, voice=args.voice)
        await prepare(args.output, backend.synthesize, voice_label=args.voice)
    print(json.dumps({'status': 'passed', 'cases': 15, 'output': str(args.output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New local artifact directory')
    parser.add_argument('--voice', choices=('male', 'female'), default='female')
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        parser.exit(1, f'Preparation failed ({type(exc).__name__}); inspect provenance.json.\n')
