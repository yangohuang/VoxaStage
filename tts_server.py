"""Local IndexTTS worker implementing VoxaStage's existing four-message contract."""
import argparse
import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from tts_runtime import BusyError


def parse_messages(messages, voices):
    common = {'session_id', 'transaction_id', 'audio_id', 'audio_sr'}
    stages = ('open_stream', 'is_start', 'text', 'is_end')
    if len(messages) != 4 or not all(isinstance(m, dict) for m in messages):
        raise ValueError('Invalid request sequence')
    first = messages[0]
    for key in ('session_id', 'transaction_id', 'audio_id'):
        if not isinstance(first.get(key), str) or not 1 <= len(first[key]) <= 128:
            raise ValueError('Invalid request identity')
    if first.get('audio_sr') != 24000 or first['audio_id'] not in voices:
        raise ValueError('Unsupported rate or voice')
    for message, stage in zip(messages, stages):
        if set(message) != common | {stage} or any(message[k] != first[k] for k in common):
            raise ValueError('Invalid request sequence or identity')
        if stage != 'text' and (type(message[stage]) is not int or message[stage] != 1):
            raise ValueError('Invalid control flag')
    text = messages[2]['text']
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError('Text must contain 1–1000 characters')
    return first['audio_id'], text.strip()


def create_app(engine=None, loader=None, *, generation_timeout=90):
    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = engine if engine is not None else await asyncio.to_thread(loader)
        try:
            yield
        finally:
            await app.state.engine.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    async def health():
        model = app.state.engine
        return {'ready': not model.closed, 'busy': model.busy, 'model': 'IndexTTS-2.5',
                'streaming': 'text-segment', 'sample_rate': 24000}

    @app.post('/api/tts/list')
    async def speakers():
        return {'speaker_ids': list(app.state.engine.voices)}

    @app.websocket('/ws/tts')
    async def synthesize(ws: WebSocket):
        await ws.accept()
        job = None
        sender = watcher = None

        async def stop_output():
            if job is not None:
                job.cancel()
            tasks = [t for t in (sender, watcher) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        async def send_audio():
            while True:
                packet = await job.receive()
                if packet is None:
                    await ws.send_json({'is_end': 1, 'code': 0})
                    return
                await ws.send_bytes(packet)

        async def watch_disconnect():
            event = await ws.receive()
            if event['type'] != 'websocket.disconnect':
                raise ValueError('One synthesis request per connection')

        try:
            messages = []
            async with asyncio.timeout(10):
                for _ in range(4):
                    event = await ws.receive()
                    if event['type'] == 'websocket.disconnect':
                        raise WebSocketDisconnect(event.get('code', 1000))
                    raw = event.get('text')
                    if not isinstance(raw, str) or len(raw) > 8192:
                        raise ValueError('Request too large')
                    messages.append(json.loads(raw))
            model = app.state.engine
            voice, text = parse_messages(messages, model.voices)
            job = model.start(voice, text)
            sender = asyncio.create_task(send_audio())
            watcher = asyncio.create_task(watch_disconnect())
            async with asyncio.timeout(generation_timeout):
                done, _ = await asyncio.wait((sender, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    job.cancel()
                    sender.cancel()
                    await watcher
                else:
                    await sender
        except WebSocketDisconnect:
            pass
        except (ValueError, TimeoutError, BusyError, RuntimeError) as exc:
            # Cancel inference delivery before an error send can yield or block.
            await stop_output()
            code = (409 if isinstance(exc, BusyError) else 504 if isinstance(exc, TimeoutError) and job is not None
                    else 400 if isinstance(exc, (ValueError, TimeoutError)) else 500)
            try:
                async with asyncio.timeout(1):
                    await ws.send_json({'error': 'Worker busy' if code == 409 else 'Invalid TTS request' if code == 400
                                       else 'Synthesis timed out' if code == 504 else 'Synthesis failed', 'code': code})
            except (RuntimeError, WebSocketDisconnect, TimeoutError):
                pass
        finally:
            await stop_output()
            try:
                async with asyncio.timeout(1):
                    await ws.close()
            except (RuntimeError, WebSocketDisconnect, TimeoutError):
                pass

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Verified official IndexTTS source directory')
    parser.add_argument('--model', required=True, help='Complete local IndexTTS 2.5 model bundle')
    parser.add_argument('--voices', required=True, help='Administrator-owned JSON voice mapping')
    parser.add_argument('--port', type=int, default=19008)
    args = parser.parse_args()
    from tts_native import load_engine
    import uvicorn
    uvicorn.run(create_app(loader=lambda: load_engine(args.source, args.model, args.voices)),
                host='127.0.0.1', port=args.port, ws_max_size=8192)


if __name__ == '__main__':
    main()
