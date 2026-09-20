"""Local conversation HTTP/WS integration without model or renderer processes."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from avatar_demo import register_avatar_routes
from avatar_profiles import ProfileRegistry
from avatar_providers import ProviderRegistry
from conversation_store import ConversationStore
from dialogue_backends import DialogueRegistry


class AvatarConversationRoutesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.root / 'avatar-web').mkdir()
        (self.root / 'avatar-web/index.html').write_text('avatar')
        (self.root / 'runtime/idle').mkdir(parents=True)
        (self.root / 'runtime/idle/kanghui-custom.mp4').write_bytes(b'configured-idle')
        (self.root / 'runtime/idle/unconfigured.mp4').write_bytes(b'not-public')
        self.registry = ProviderRegistry(self.root / 'providers.json', env={
            'PIPECAT_DINET_URL': 'ws://renderer', 'PIPECAT_AVATAR_PROVIDER': 'dinet'})
        config = self.root / 'profiles.json'
        config.write_text(json.dumps({'version': 1, 'default_profile': 'kanghui', 'profiles': [
            {'id': 'kanghui', 'label': 'Kanghui', 'provider': 'dinet', 'voice': 'female',
             'idle_url': '/avatar/idle/kanghui-custom.mp4'},
            {'id': 'other', 'label': 'Other', 'provider': 'streamingtalker', 'voice': 'male',
             'idle_url': '/avatar/idle/streamingtalker.mp4'}]}))
        self.profiles = ProfileRegistry(self.registry, config)
        self.dialogue = DialogueRegistry(self.root / 'dialogue.json', env={'PIPECAT_MINICPM_URL': 'http://worker'})
        self.store = ConversationStore(self.root / 'runtime')
        self.active, self.workers = set(), {}
        self.llm = SimpleNamespace(configure_agent=self.configure_agent, cancel_agent=lambda: None)
        self.configured_session = None
        self.voice = None
        self.transport = None
        outer = self

        class Transport:
            def __init__(self, websocket, params):
                self.websocket, self.handlers = websocket, {}
                outer.transport = self
            def input(self): return object()
            def output(self): return object()
            def event_handler(self, name):
                def register(fn):
                    self.handlers[name] = fn
                    return fn
                return register

        class Runner:
            def __init__(self, **kwargs): pass
            async def add_workers(self, worker): pass
            async def run(self):
                transport = outer.transport
                await transport.handlers['on_client_connected'](transport, transport.websocket)
                try:
                    while True:
                        message = await transport.websocket.receive_json()
                        session = outer.configured_session
                        session.history.begin(message['text'], session.generation)
                        session.history.text('Remembered.', session.generation)
                        await session.persist_history()
                        await session.send({'type': 'saved'})
                except WebSocketDisconnect:
                    return

        async def cancel(): pass
        for name, value in {
            'ROOT': self.root, 'ProviderRegistry': lambda: self.registry,
            'ProfileRegistry': lambda registry: self.profiles,
            'DialogueRegistry': lambda: self.dialogue,
            'ConversationStore': lambda path: self.store,
            'FastAPIWebsocketTransport': Transport, 'WorkerRunner': Runner,
            'Pipeline': lambda items: items,
            'PipelineWorker': lambda *args, **kwargs: SimpleNamespace(cancel=cancel),
            'TurnObserver': lambda *args, **kwargs: object(),
        }.items():
            self.stack.enter_context(patch('avatar_demo.' + name, value, create=True))
        self.app = FastAPI()
        register_avatar_routes(self.app, self.make_components, self.active, self.workers)
        self.client = self.stack.enter_context(TestClient(self.app))

    def configure_agent(self, session, history):
        self.configured_session = session
        self.assertIs(session.history, history)

    def make_components(self, backend):
        self.voice = backend.voice
        aggregators = SimpleNamespace(user=lambda: object(), assistant=lambda: object())
        return object(), aggregators, self.llm, object()

    def create(self, **overrides):
        result = self.client.post('/avatar/conversations', json={
            'backend': 'cascade', 'profile_id': 'kanghui', **overrides})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_http_crud_and_profile_metadata(self):
        record = self.create()
        self.dialogue.url = ''
        metadata = self.client.get('/avatar/providers').json()
        self.assertEqual(metadata['default_profile'], 'kanghui')
        self.assertEqual(metadata['profiles'][0]['idle_url'], '/avatar/idle/kanghui-custom.mp4')
        self.assertTrue(metadata['dialogue_backends'][0]['tools'])
        self.assertFalse(metadata['dialogue_backends'][1]['tools'])
        self.assertEqual(self.client.get('/avatar/conversations/' + record['id']).json(), record)
        listed = self.client.get('/avatar/conversations').json()['conversations']
        self.assertEqual(listed[0]['profile_id'], 'kanghui')
        self.assertNotIn('snapshot', listed[0])
        self.assertEqual(self.client.get('/avatar/idle/kanghui-custom.mp4').content, b'configured-idle')
        self.assertEqual(self.client.get('/avatar/idle/unconfigured.mp4').status_code, 404)
        self.assertEqual(self.client.delete('/avatar/conversations/' + record['id']).json(), {'deleted': True})
        self.assertEqual(self.client.get('/avatar/conversations/' + record['id']).status_code, 404)

    def test_mutations_reject_cross_origin_and_unconfigured_choices(self):
        record = self.create()
        for origin in ('https://evil.example', 'http://testserver.evil'):
            self.assertEqual(self.client.post('/avatar/conversations', json={
                'backend': 'cascade', 'profile_id': 'kanghui'}, headers={'Origin': origin}).status_code, 403)
            self.assertEqual(self.client.delete('/avatar/conversations/' + record['id'],
                                               headers={'Origin': origin}).status_code, 403)
        for body in ({'backend': 'unknown', 'profile_id': 'kanghui'},
                     {'backend': 'cascade', 'profile_id': 'missing'},
                     {'backend': 'cascade', 'profile_id': 'kanghui', 'url': 'http://evil'},
                     {'backend': [], 'profile_id': 'kanghui'}):
            self.assertEqual(self.client.post('/avatar/conversations', json=body).status_code, 400)
        self.assertEqual(self.client.post('/avatar/conversations', content=b'x' * 5000).status_code, 413)
        self.assertEqual(self.client.post('/avatar/conversations', content=b'[' * 1500 + b']' * 1500).status_code, 400)
        self.assertEqual(self.client.get('/avatar/conversations/not-an-id').status_code, 400)

    def test_resume_mismatch_rejected_before_pipeline_and_origin_checked(self):
        record = self.create()
        for query in ('backend=minicpm', 'profile=other', 'provider=streamingtalker'):
            with self.client.websocket_connect('/avatar/ws?conversation=' + record['id'] + '&' + query) as ws:
                self.assertEqual(ws.receive_json()['type'], 'error')
        self.assertIsNone(self.configured_session)
        self.assertFalse(self.active)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/avatar/ws', headers={'Origin': 'https://evil.example'}):
                pass

    def test_websocket_restore_active_delete_and_disconnect_persistence(self):
        record = self.create()
        with self.client.websocket_connect('/avatar/ws?conversation=' + record['id']) as ws:
            ready = ws.receive_json()
            self.assertEqual(ready['type'], 'ready', ready)
            self.assertEqual(ready['conversation_id'], record['id'])
            self.assertEqual(ready['profile_id'], 'kanghui')
            self.assertTrue(ready['tools'])
            self.assertEqual(ready['transcript'], [])
            self.assertEqual(self.voice, 'female')
            self.assertEqual(self.client.delete('/avatar/conversations/' + record['id']).status_code, 409)
            ws.send_json({'text': 'My favorite color is blue'})
            self.assertEqual(ws.receive_json()['type'], 'saved')
        saved = self.client.get('/avatar/conversations/' + record['id']).json()
        self.assertEqual(saved['title'], 'My favorite color is blue')
        self.assertEqual(saved['transcript'][1]['status'], 'interrupted')
        self.assertFalse(self.active)
        with self.client.websocket_connect('/avatar/ws?conversation=' + record['id']) as ws:
            ready = ws.receive_json()
            self.assertEqual(ready['transcript'][0]['text'], 'My favorite color is blue')
            self.assertEqual(self.configured_session.history.context()[0]['content'], 'My favorite color is blue')
        self.assertEqual(self.client.delete('/avatar/conversations/' + record['id']).status_code, 200)

    def test_cancelled_save_updates_revision_before_releasing_save_lock(self):
        record = self.create()
        with self.client.websocket_connect('/avatar/ws?conversation=' + record['id']) as ws:
            self.assertEqual(ws.receive_json()['type'], 'ready')
            async def exercise():
                import threading
                started, release, finished = threading.Event(), threading.Event(), threading.Event()
                original_save = self.store.save
                def slow_save(*args, **kwargs):
                    started.set()
                    release.wait(timeout=5)
                    try:
                        return original_save(*args, **kwargs)
                    finally:
                        finished.set()
                session = self.configured_session
                with patch.object(self.store, 'save', slow_save):
                    task = asyncio.create_task(session.persist_history())
                    await asyncio.to_thread(started.wait, 5)
                    task.cancel()
                    release.set()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    await asyncio.to_thread(finished.wait, 5)
                await session.persist_history()
            self.client.portal.call(exercise)

    def test_legacy_provider_auto_creates_conversation_and_exclusive_admission(self):
        with self.client.websocket_connect('/avatar/ws?provider=dinet&backend=cascade') as ws:
            ready = ws.receive_json()
            self.assertEqual(ready['type'], 'ready', ready)
            self.assertEqual(ready['profile_id'], 'kanghui')
            with self.client.websocket_connect('/avatar/ws?profile=kanghui') as other:
                self.assertEqual(other.receive_json()['type'], 'error')
        self.assertEqual(len(self.store.list()), 1)
        self.assertFalse(self.active)
        self.assertFalse(self.workers)


if __name__ == '__main__':
    unittest.main()
