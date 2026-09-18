"""Verify an actual 2D model bridge with PCM preservation and cancel/recovery.

Use a supplied PCM16 mono WAV; this script never substitutes a fake renderer.
Saved images and metrics are local verification artifacts, not release assets.
"""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import time
import wave

import numpy as np
import soxr

from avatar_video_backend import VideoBackend
from dinet_backend import DINetBackend


def read_pcm(path):
    with wave.open(str(path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError('Input must be a mono PCM16 WAV')
        if not 8000 <= wav.getframerate() <= 48000 or not 0 < wav.getnframes() <= wav.getframerate() * 30:
            raise ValueError('Expected 0–30 seconds of 8–48 kHz input')
        pcm = wav.readframes(wav.getnframes())
        if wav.getframerate() != 24000:
            pcm = soxr.resample(np.frombuffer(pcm, '<i2'), wav.getframerate(), 24000).astype('<i2').tobytes()
        return pcm


async def trial(args, pcm, label, *, interrupt=False):
    started = time.monotonic()
    async def source():
        for offset in range(0, len(pcm), 12000):
            yield pcm[offset:offset + 12000]
            if args.realtime:
                await asyncio.sleep(len(pcm[offset:offset + 12000]) / 48000)

    backend = (DINetBackend if args.backend == 'dinet' else VideoBackend)(args.url)
    output = bytearray()
    frames = 0
    first_image = last_image = None
    report = dict(label=label, interrupted=interrupt)
    stream = backend.stream(source())
    ended = False
    try:
        async with asyncio.timeout(args.timeout):
            async for event in stream:
                if event['type'] == 'avatar_meta':
                    report['metadata'] = event
                    report['metadata_s'] = round(time.monotonic() - started, 3)
                elif event['type'] == 'media':
                    assert event['frame_index'] == frames
                    assert event['start_sample'] == len(output) // 2
                    output.extend(base64.b64decode(event['audio'], validate=True))
                    image = base64.b64decode(event['image'], validate=True)
                    if frames == 0:
                        report['first_media_s'] = round(time.monotonic() - started, 3)
                        first_image = image
                    last_image = image
                    frames += 1
                    if interrupt:
                        break
                elif event['type'] == 'clip_end':
                    assert event['total_samples'] == len(pcm) // 2
                    assert bytes(output) == pcm, 'Model adapter changed the original audio'
                    ended = True
    finally:
        await stream.aclose()
    assert frames > 0 and (interrupt or ended), 'Bridge ended without verified media'
    assert bytes(output) == pcm[:len(output)], 'Interrupted audio differs from original prefix'
    args.output.mkdir(parents=True, exist_ok=True)
    for name, image in [('first', first_image), ('last', last_image)]:
        (args.output / f'{label}-{name}.jpg').write_bytes(image)
    report.update(frames=frames, total_samples=len(output) // 2,
                  wall_s=round(time.monotonic() - started, 3),
                  first_image_sha256=hashlib.sha256(first_image).hexdigest(),
                  last_image_sha256=hashlib.sha256(last_image).hexdigest())
    return report


async def run(args):
    pcm = read_pcm(args.input)
    results = []
    args.output.mkdir(parents=True, exist_ok=True)
    report = dict(passed=False, status='running', input_samples=len(pcm) // 2, rounds=results,
                  scope='Actual model bridge PCM/JPEG; not physical microphone, playback, or visual quality validation')
    def record():
        (args.output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    record()  # Never leave a previous successful report after a failed rerun.
    cases = [(f'round-{number + 1}', False) for number in range(args.rounds)]
    if args.interrupt:
        cases += [('interrupted', True), ('recovered', False)]
    try:
        for label, interrupt in cases:
            result = await trial(args, pcm, label, interrupt=interrupt)
            results.append(result)
            record()
            print(json.dumps(result, ensure_ascii=False), flush=True)
    except BaseException as exc:
        report.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
        record()
        raise
    report.update(status='passed', passed=True)
    record()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:8203/v1/stream')
    parser.add_argument('--backend', choices=('bridge', 'dinet'), default='bridge')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('artifacts/flashhead-model'))
    parser.add_argument('--rounds', type=int, default=3, choices=range(1, 11))
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--interrupt', action='store_true')
    parser.add_argument('--realtime', action='store_true')
    asyncio.run(run(parser.parse_args()))
