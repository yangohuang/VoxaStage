"""Loopback-only fixed-PCM playback lab; reuses production adapters and browser player."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from aiohttp import web
from avatar_eval_metrics import MediaMeasurement
from avatar_providers import ProviderRegistry
from benchmark_avatar import close_stream, load_cases

ROOT = Path(__file__).resolve().parent


def make_app(manifest, config=None, *, factories=None, timeout=90):
    if not 0 < timeout <= 300:
        raise ValueError('Invalid timeout')
    with Path(manifest).open('rb') as source:
        manifest_bytes = source.read(2_000_001)
    cases = {case.id: case for case in load_cases(manifest, manifest_bytes=manifest_bytes)}
    if factories is None:
        registry = ProviderRegistry(config)
        factories = {key: value.make_backend for key, value in registry.providers.items() if value.url}
    busy = asyncio.Lock()
    @web.middleware
    async def loopback_host(request, handler):
        port = request.transport.get_extra_info('sockname')[1]
        if request.host != f'127.0.0.1:{port}':
            raise web.HTTPForbidden(text='Loopback host required')
        return await handler(request)
    app = web.Application(client_max_size=4096, middlewares=[loopback_host])

    async def configuration(request):
        paths = ['avatar-web/audio.mjs', 'avatar-web/timeline.mjs', 'avatar-web/renderer.mjs',
                 'evaluation/playback.mjs', 'avatar_playback_lab.py']
        return web.json_response(dict(cases=[case.public() for case in cases.values()],
             providers=list(factories), manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
             source_sha256={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}))

    async def stream(request):
        if request.headers.get('Origin') != f'http://{request.host}':
            raise web.HTTPForbidden(text='Same-origin browser required')
        provider, case_id = request.query.get('provider'), request.query.get('case')
        if provider not in factories or case_id not in cases:
            raise web.HTTPBadRequest(text='Unknown provider or case')
        if busy.locked():
            raise web.HTTPConflict(text='A lab request is already active')
        async with busy:
            ws = web.WebSocketResponse(max_msg_size=4096)
            await ws.prepare(request)
            case = cases[case_id]
            started = time.monotonic()
            async def send(event):
                await ws.send_json(event | dict(adapter_elapsed_ms=(time.monotonic()-started)*1000))
            async def source():
                for offset in range(0, len(case.pcm), 12000):
                    yield case.pcm[offset:offset+12000]
            async def produce():
                try:
                    meter = MediaMeasurement(case.pcm)
                    output = factories[provider]().stream(source())
                    try:
                        async with asyncio.timeout(timeout):
                            async for event in output:
                                meter.add(event, time.monotonic()-started)
                                await send(event)
                    finally:
                        await close_stream(output)
                    meter.finish(time.monotonic()-started)
                    await send(dict(type='stream_end', status='passed', pcm_sha256=case.pcm_sha256))
                except Exception as exc:
                    if not ws.closed:
                        await send(dict(type='stream_end', status='failed', error_type=type(exc).__name__))
                finally:
                    await ws.close()
            task = asyncio.create_task(produce())
            try:
                async for message in ws:
                    # Evaluation connections carry no browser audio or commands.
                    await ws.close()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return ws

    app.router.add_get('/config', configuration)
    app.router.add_get('/ws', stream)
    for route, path in [('/', ROOT/'evaluation/playback.html')] + [
            ('/assets/'+name, ROOT/'avatar-web'/name) for name in ('audio.mjs','timeline.mjs','renderer.mjs')] + [
            ('/playback.mjs', ROOT/'evaluation/playback.mjs')]:
        async def file(request, path=path):
            return web.FileResponse(path, headers={'Cache-Control':'no-store'})
        app.router.add_get(route, file)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--port', type=int, default=18424)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be1024–65535')
    try:
        app = make_app(args.manifest, args.config)
    except Exception as exc:
        parser.exit(1, f'Lab configuration failed ({type(exc).__name__}).\n')
    web.run_app(app, host='127.0.0.1', port=args.port, access_log=None)


if __name__ == '__main__':
    main()
