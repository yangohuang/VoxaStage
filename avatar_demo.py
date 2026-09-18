"""Optional avatar entry point, sharing single-session admission with voice."""
from pathlib import Path
from uuid import uuid4

import httpx
import anyio
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from avatar_providers import ProviderRegistry
from avatar_session import AvatarInput, AvatarOutput, AvatarSerializer, AvatarSession, AvatarText
from backend import LocalBackend
from dialogue_backends import DialogueRegistry
from omni_backend import OmniBackend
from omni_processor import OmniProcessor
from turn_observer import TurnObserver

ROOT = Path(__file__).resolve().parent


def register_avatar_routes(app, make_components, active_sessions, active_workers):
    registry = ProviderRegistry()
    dialogue = DialogueRegistry()
    app.mount('/avatar/static', StaticFiles(directory=ROOT / 'avatar-web'), name='avatar-static')

    @app.get('/avatar/providers')
    async def providers():
        public=dialogue.public()
        for backend in public['dialogue_backends']:
            backend['available']=backend['configured']
            if backend['id']=='minicpm' and backend['configured']:
                try:
                    async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
                        response=await client.get(dialogue.url.rstrip('/')+'/health')
                        backend['available']=response.status_code==200 and response.json().get('ready') is True
                except (httpx.HTTPError,ValueError):
                    backend['available']=False
        return {**registry.public(), **public}

    @app.get('/avatar')
    async def page():
        return FileResponse(ROOT / 'avatar-web' / 'index.html')

    @app.get('/avatar/idle/{provider_id}.mp4')
    async def idle_video(provider_id: str):
        if provider_id not in ('dinet','flashhead','streamingtalker'):
            raise HTTPException(404)
        path=ROOT/'runtime'/'idle'/f'{provider_id}.mp4'
        if not path.is_file():
            raise HTTPException(404, 'Idle video has not been prepared')
        return FileResponse(path, media_type='video/mp4', headers={'Cache-Control':'public, max-age=3600'})

    @app.websocket('/avatar/ws')
    async def websocket_endpoint(websocket: WebSocket):
        # Browser requests must be same-origin. A missing Origin is allowed for
        # local CLI smoke tests; this demo deliberately has no public auth layer.
        origin = websocket.headers.get('origin')
        expected = f'{"https" if websocket.url.scheme == "wss" else "http"}://{websocket.headers.get("host")}'
        if origin is not None and origin != expected:
            await websocket.close(code=1008)
            return
        try:
            provider = registry.resolve(websocket.query_params.get('provider'))
            dialogue_id = dialogue.resolve(websocket.query_params.get('backend'))
        except ValueError as exc:
            await websocket.accept()
            await websocket.send_json({'type': 'error', 'message': str(exc)})
            await websocket.close(code=1008)
            return
        if active_sessions:
            await websocket.accept()
            await websocket.send_json({'type': 'error', 'message': '已有会话运行，请先断开另一个页面。'})
            await websocket.close(code=1013)
            return
        key = 'avatar-' + uuid4().hex[:12]
        active_sessions.add(key)
        session = None
        omni = None
        try:
            await websocket.accept()
            session = AvatarSession(websocket, provider.make_backend())
            async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
                transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
                    audio_in_enabled=True, audio_out_enabled=False,
                    audio_in_sample_rate=16000, audio_out_sample_rate=24000,
                    serializer=AvatarSerializer(), session_timeout=600))
                if dialogue_id == 'minicpm':
                    health = await client.get(dialogue.url.rstrip('/') + '/health', timeout=5)
                    health.raise_for_status()
                    if health.json().get('ready') is not True:
                        raise RuntimeError('MiniCPM is not ready')
                    omni = OmniProcessor(session, OmniBackend(client, dialogue.url, voice=provider.voice))
                    processors = [transport.input(), omni, transport.output()]
                    observers = []
                else:
                    stt, aggregators, llm, tts = make_components(LocalBackend(client, voice=provider.voice))
                    processors = [transport.input(), stt, AvatarInput(session), aggregators.user(), llm,
                                  AvatarText(session), tts, AvatarOutput(session), transport.output(), aggregators.assistant()]
                    observers = [TurnObserver(key, llm=llm)]
                worker = PipelineWorker(Pipeline(processors),
                    params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
                    idle_timeout_secs=600, observers=observers, enable_rtvi=False)
                active_workers[key] = worker

                @transport.event_handler('on_client_connected')
                async def connected(transport, socket):
                    await session.send({'type': 'ready', 'session_id': key,
                                        'provider': provider.id, 'kind': provider.kind,
                                        'dialogue_backend': dialogue_id,
                                        'input_sample_rate': 16000, 'generation': session.generation})

                @transport.event_handler('on_client_disconnected')
                async def disconnected(transport, socket):
                    await worker.cancel()

                @transport.event_handler('on_session_timeout')
                async def timeout(transport, socket):
                    await worker.cancel()

                runner = WorkerRunner(handle_sigint=False)
                await runner.add_workers(worker)
                await runner.run()
        except Exception:
            logger.exception('Avatar session failed')
            if session is not None:
                try:
                    await session.send({'type':'error','message':'对话后端连接失败，请检查模型服务。'})
                except Exception:
                    pass
        finally:
            # ASGI disconnect cancellation must not interrupt model/renderer
            # cleanup or leave the shared single-session admission occupied.
            with anyio.CancelScope(shield=True):
                try:
                    if omni is not None:
                        await omni.conversation.close()
                    if session is not None:
                        await session.close()
                finally:
                    active_workers.pop(key, None)
                    active_sessions.discard(key)
                try:
                    await websocket.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass
