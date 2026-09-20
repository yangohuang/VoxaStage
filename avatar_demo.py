"""Optional avatar entry point, sharing single-session admission with voice."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import anyio
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from avatar_profiles import ProfileRegistry
from avatar_providers import ProviderRegistry
from avatar_session import AvatarInput, AvatarOutput, AvatarSerializer, AvatarSession, AvatarText
from backend import LocalBackend
from conversation_store import (ConversationStore, ConversationError, ConversationNotFound,
                                ConversationConflict, ConversationLimit)
from dialogue_backends import DialogueRegistry
from playback_history import PlaybackHistory
from omni_backend import OmniBackend
from omni_processor import OmniProcessor
from turn_observer import TurnObserver

ROOT = Path(__file__).resolve().parent


def register_avatar_routes(app, make_components, active_sessions, active_workers):
    registry = ProviderRegistry()
    dialogue = DialogueRegistry()
    profiles = ProfileRegistry(registry)
    store = ConversationStore(os.environ.get('PIPECAT_CONVERSATION_DIR', ROOT / 'runtime'))
    active_conversations = set()
    admission_lock = asyncio.Lock()

    def same_origin(request):
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.headers.get("host")}'
        if origin is not None and origin != expected:
            raise HTTPException(403, 'Conversation changes require the same origin')

    def storage_error(exc):
        code = (404 if isinstance(exc, ConversationNotFound) else
                409 if isinstance(exc, (ConversationConflict, ConversationLimit)) else 400)
        return HTTPException(code, str(exc))

    def select_profile(profile_id=None, provider_id=None):
        if profile_id is None and provider_id is not None:
            # Preserve the old driver query while profiles can use independent IDs.
            candidates = [item for item in profiles.profiles.values() if item.provider == provider_id]
            if not candidates:
                raise ValueError('未知数字人驱动，请重新选择。')
            profile_id = next((item.id for item in candidates if item.id == profiles.default), candidates[0].id)
        profile = profiles.resolve(profile_id)
        if provider_id is not None and provider_id != profile.provider:
            raise ValueError('角色与数字人驱动不匹配。')
        return profile

    @app.get('/avatar/conversations')
    async def conversations():
        try:
            return {'conversations': await asyncio.to_thread(store.list)}
        except ConversationError as exc:
            raise storage_error(exc) from exc

    @app.post('/avatar/conversations')
    async def create_conversation(request: Request):
        same_origin(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, 'Conversation request is too large')
        try:
            body = json.loads(raw)
            if (not isinstance(body, dict) or set(body) != {'backend', 'profile_id'}
                    or not all(isinstance(value, str) and len(value) <= 64 for value in body.values())):
                raise ValueError('Invalid conversation choices')
            backend = dialogue.resolve(body['backend'])
            profile = profiles.resolve(body['profile_id'])
            return await asyncio.to_thread(store.create, backend=backend, profile_id=profile.id)
        except ConversationError as exc:
            raise storage_error(exc) from exc
        except (ValueError, RecursionError) as exc:
            raise HTTPException(400, 'Invalid conversation request') from exc

    @app.get('/avatar/conversations/{conversation_id}')
    async def load_conversation(conversation_id: str):
        try:
            return await asyncio.to_thread(store.load, conversation_id)
        except ConversationError as exc:
            raise storage_error(exc) from exc

    @app.delete('/avatar/conversations/{conversation_id}')
    async def delete_conversation(conversation_id: str, request: Request):
        same_origin(request)
        async with admission_lock:
            if conversation_id in active_conversations:
                raise HTTPException(409, '请先断开正在使用的对话。')
            try:
                return {'deleted': await asyncio.to_thread(store.delete, conversation_id)}
            except ConversationError as exc:
                raise storage_error(exc) from exc
    app.mount('/avatar/static', StaticFiles(directory=ROOT / 'avatar-web'), name='avatar-static')

    @app.get('/avatar/providers')
    async def providers():
        public=dialogue.public()
        for backend in public['dialogue_backends']:
            backend['available']=backend['configured']
            backend['image_input']=False
            backend['tools']=backend['id']=='cascade'
            if backend['id']=='minicpm' and backend['configured']:
                try:
                    async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
                        response=await client.get(dialogue.url.rstrip('/')+'/health')
                        backend['available']=response.status_code==200 and response.json().get('ready') is True
                        backend['image_input']=backend['available'] and response.json().get('capabilities',{}).get('image_input') is True
                except (httpx.HTTPError,ValueError):
                    backend['available']=False
        return {**registry.public(), **profiles.public(), **public}

    @app.get('/avatar')
    async def page():
        return FileResponse(ROOT / 'avatar-web' / 'index.html')

    @app.get('/avatar/idle/{provider_id}.mp4')
    async def idle_video(provider_id: str):
        allowed = {'/avatar/idle/' + name + '.mp4' for name in ('dinet', 'flashhead', 'streamingtalker')}
        allowed.update(profile.idle_url for profile in profiles.profiles.values())
        if f'/avatar/idle/{provider_id}.mp4' not in allowed:
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
        key = 'avatar-' + uuid4().hex[:12]
        try:
            async with admission_lock:
                if active_sessions:
                    await websocket.accept()
                    await websocket.send_json({'type': 'error', 'message': '已有会话运行，请先断开另一个页面。'})
                    await websocket.close(code=1013)
                    return
                conversation_id = websocket.query_params.get('conversation')
                if conversation_id is not None:
                    record = await asyncio.to_thread(store.load, conversation_id)
                    profile_id = websocket.query_params.get('profile', record['profile_id'])
                    dialogue_id = dialogue.resolve(websocket.query_params.get('backend', record['backend']))
                    if profile_id != record['profile_id'] or dialogue_id != record['backend']:
                        raise ValueError('保存的对话与所选角色或对话后端不匹配。')
                    profile = select_profile(profile_id, websocket.query_params.get('provider'))
                else:
                    profile = select_profile(websocket.query_params.get('profile'), websocket.query_params.get('provider'))
                    dialogue_id = dialogue.resolve(websocket.query_params.get('backend'))
                    record = await asyncio.to_thread(store.create, backend=dialogue_id, profile_id=profile.id)
                    conversation_id = record['id']
                # The shared voice admission can change while the file is read.
                if active_sessions:
                    raise ValueError('已有会话运行，请先断开另一个页面。')
                provider = registry.providers[profile.provider]
                history = PlaybackHistory(backend=dialogue_id, history=record['snapshot']['history'],
                                          transcript=record['transcript'])
                active_sessions.add(key)
                active_conversations.add(conversation_id)
        except ValueError as exc:
            await websocket.accept()
            await websocket.send_json({'type': 'error', 'message': str(exc)})
            await websocket.close(code=1008)
            return
        persist_lock = asyncio.Lock()

        async def persist():
            nonlocal record
            async with persist_lock:
                transcript = session.history.transcript()
                title = record['title']
                if title == 'New conversation':
                    title = next((item['text'].strip()[:80] for item in transcript
                                  if item['role'] == 'user' and item['text'].strip()), title)
                # A cancelled to_thread await does not stop its disk write.
                # Finish it and update the revision before releasing the lock,
                # otherwise the next turn can race a successful old save.
                cancelled = False
                with anyio.CancelScope(shield=True):
                    saving = asyncio.create_task(asyncio.to_thread(store.save, conversation_id,
                        snapshot=session.history.snapshot(), transcript=transcript,
                        title=title, expected_revision=record['revision']))
                    while True:
                        try:
                            record = await asyncio.shield(saving)
                            break
                        except asyncio.CancelledError:
                            if saving.cancelled():
                                raise
                            cancelled = True
                if cancelled:
                    raise asyncio.CancelledError

        session = None
        omni = None
        visual_enabled = False
        try:
            await websocket.accept()
            session = AvatarSession(websocket, profile.make_backend(registry), history=history, persist=persist)
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
                    visual_enabled = health.json().get('capabilities',{}).get('image_input') is True
                    omni = OmniProcessor(session, OmniBackend(client, dialogue.url, voice=profile.voice),
                                         visual_enabled=visual_enabled, session_id=key)
                    processors = [transport.input(), omni, transport.output()]
                    observers = []
                else:
                    stt, aggregators, llm, tts = make_components(LocalBackend(client, voice=profile.voice))
                    llm.configure_agent(session, session.history)
                    session.cancel_agent = llm.cancel_agent
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
                                        'conversation_id': conversation_id, 'profile_id': profile.id,
                                        'idle_url': profile.idle_url, 'tools': dialogue_id == 'cascade',
                                        'transcript': session.history.transcript(),
                                        'image_input': visual_enabled,
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
                    active_conversations.discard(conversation_id)
                try:
                    await websocket.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass
