"""Exercise real avatar models with text or an explicitly supplied PCM16 WAV."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import time
import wave

import websockets


async def run(args):
    events, counts, first_media = [], {}, {}
    async with websockets.connect(args.url, proxy=None, max_size=2_000_000) as ws:
        ready = json.loads(await ws.recv())
        assert ready['type'] == 'ready', ready
        generation = ready['generation']
        async def send(message):
            await ws.send(json.dumps(message, ensure_ascii=False))
        started = time.monotonic()
        async def input_audio():
            with wave.open(str(args.input)) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                pcm = wav.readframes(wav.getnframes())
            for position in range(0, len(pcm), 640):
                await ws.send(pcm[position:position+640])
                await asyncio.sleep(.02)
            # VAD needs trailing silence to close the utterance.
            for _ in range(60):
                await ws.send(bytes(640))
                await asyncio.sleep(.02)
        sender = asyncio.create_task(input_audio()) if args.input else None
        if sender is None:
            await send({'type': 'text', 'text': args.text})
        interrupted = False
        try:
            async with asyncio.timeout(75):
                while True:
                    event = json.loads(await ws.recv())
                    kind = event['type']
                    if kind == 'reset':
                        assert event['generation'] > generation
                        generation = event['generation']
                    if kind in ('avatar_meta', 'media', 'clip_end'):
                        assert event['generation'] == generation, 'Old-generation media crossed reset'
                    if kind == 'media':
                        clip = event['clip_id']
                        size = len(base64.b64decode(event['audio'])) // 2
                        assert event['start_sample'] == counts.get(clip, 0)
                        counts[clip] = counts.get(clip, 0) + size
                        if generation not in first_media:
                            first_media[generation] = round(time.monotonic() - started, 3)
                            await send({'type': 'playback', 'generation': generation, 'state': 'started'})
                            if args.interrupt and not interrupted:
                                interrupted = True
                                if args.input:
                                    await sender
                                    sender = asyncio.create_task(input_audio())
                                else:
                                    await send({'type': 'interrupt'})
                                    await send({'type': 'text', 'text': '请只说你好。'})
                        continue
                    events.append({k: v for k, v in event.items() if k != 'faces'})
                    if kind == 'error':
                        raise RuntimeError(event)
                    if kind == 'clip_end':
                        assert counts[event['clip_id']] == event['total_samples']
                        if not args.interrupt or len(first_media) >= 2:
                            await send({'type': 'playback', 'generation': generation, 'state': 'ended'})
                            break
            if sender is not None:
                await sender
        finally:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
    result = {'passed': True, 'input': 'wav' if args.input else 'text', 'interrupted': interrupted,
              'first_media_since_request_s': first_media, 'samples_per_clip': counts, 'events': events,
              'scope': 'websocket received paired media; does not prove physical speaker playback'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'events'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:18314/avatar/ws')
    parser.add_argument('--text', default='介绍一下语音agent技术')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--interrupt', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('artifacts/avatar-smoke.json'))
    asyncio.run(run(parser.parse_args()))
